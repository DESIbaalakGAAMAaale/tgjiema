"""R63 P0-02 / P0-03 / P0-06 / P1-04: 恢复信任链深冻结 + 原子性 + CLI 路径 + 开放事务修复。

测试覆盖:
1. **P0-02**: VerifiedBackupPayload 深冻结 + writer 端重算 digest
   - tables / payload 为 MappingProxyType(深冻结,非浅冻结)
   - 嵌套 dict / list 递归冻结(MappingProxyType / tuple)
   - 别名引用保护(deepcopy 断绝与调用方原 dict 的引用)
   - ``object.__setattr__`` 绕过 frozen 替换 payload → writer 重算 digest fail-closed
   - ``_compute_payload_digest`` / ``_to_serializable`` 处理冻结结构(与普通 dict 一致)
   - **篡改验收**: 构造 VerifiedBackupPayload 后修改原始 dict,writer 应 fail-closed
     (若通过 object.__setattr__ 替换 payload,重算 digest 不匹配 capability → AppError)

2. **P0-03**: 恢复非跨数据源原子操作 — 任一数据源失败 → AppError(不返回部分成功)
   - CRDB 恢复失败 → AppError
   - SQLite 恢复失败 → AppError
   - relay_sqlite 恢复失败 → AppError
   - result["errors"] 非空 → AppError(belt-and-suspenders 检查)

3. **P0-06**: CLI 恢复路径与三段式备份发现模型一致
   - run_restore 必须传入 backup_id(三段式发现入口)
   - run_restore 不再调用 get_latest_backup(双重 loader 已删除)
   - run_restore 传 data=None 给 strict service(由 service 自行解密 payload)
   - run_restore 计算并传入 expected_manifest_key

4. **P1-04**: restore 遗留开放事务
   - cols 为空 → raise AppError(不 continue,不 BEGIN,无开放事务)
   - 使用 ``async with db_client.transaction() as conn:`` context manager
   - 事务内异常 → AppError(context manager 自动 ROLLBACK,fail-closed)
   - schema/column 校验全部在 BEGIN 前完成
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 测试辅助(与 test_r62_p0_01_02_restore_capability.py 同构) ──────────


def _ensure_backup_dr_validate_importable():
    """确保 services.backup_dr_validate 可导入(仅依赖 loguru / i18n)。"""
    if "services.backup_dr_validate" in sys.modules:
        return sys.modules["services.backup_dr_validate"]
    import importlib
    return importlib.import_module("services.backup_dr_validate")


def _ensure_restore_module_importable():
    """确保 services.db_restore 可导入(mock database.session / storage.r2 依赖)。"""
    if "services.db_restore" in sys.modules:
        return sys.modules["services.db_restore"]

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
        mock_cs.DB_PATH = Path("/tmp/fake_cache_store_r63.db")
        sys.modules["database.cache_store"] = mock_cs
        setattr(sys.modules["database"], "cache_store", mock_cs)

    if "database.relay_db" not in sys.modules:
        mock_rdb = types.ModuleType("database.relay_db")
        mock_rdb.DB_PATH = Path("/tmp/fake_relay_pool_r63.db")
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
    schema_fingerprint: str = "R63-P0-02-test-fingerprint",
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


def _build_verified_payload(
    mod,
    *,
    tables: dict | None = None,
    payload: dict | None = None,
    schema_fingerprint: str = "R63-P0-02-test-fingerprint",
    backup_id: str = "backup_test_001",
):
    """构造 VerifiedBackupPayload(单一 canonical bytes 来源,自动计算 payload_digest)。

    R64 P1-01: payload/tables 改为从 canonical_payload_bytes 解码的 property,
    不再是独立字段。tables/payload 参数仅用于决定 canonical bytes 的内容
    (若同时传入,以 payload 为准,tables 字段应嵌入 payload["tables"])。
    """
    if tables is None:
        tables = {"users": [{"user_id": 1, "username": "alice"}]}
    if payload is None:
        payload = {"tables": tables}
    return mod.VerifiedBackupPayload(
        backup_id=backup_id,
        manifest_sha256="a" * 64,
        plaintext_sha256="c" * 64,
        schema_fingerprint=schema_fingerprint,
        canonical_payload_bytes=mod._canonical_json_bytes(payload),
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


async def _patch_store(monkeypatch):
    """R63 P1-01: 创建 fresh CacheStore 并 patch get_cache_store 返回它。

    用于调用真实 ``_restore_from_backup_data`` 的测试 — 该函数内部调用
    ``assert_valid``(无 store= 参数),``assert_valid`` 会通过 ``get_cache_store()``
    获取单例。注入真实 CacheStore 以完成 nonce 原子消费。
    """
    store = await _fresh_store()
    import database.cache_store as _cs_mod
    monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: store)
    return store


# ═══════════════════════════════════════════════════════════════
# P0-02: 深冻结辅助函数(_deep_freeze / _freeze_recursive / _to_serializable)
# ═══════════════════════════════════════════════════════════════


class TestDeepFreezeHelpers:
    """R63 P0-02: _deep_freeze / _freeze_recursive / _to_serializable 单元测试。"""

    def test_deep_freeze_dict_returns_mapping_proxy(self):
        """_deep_freeze(dict) 返回 MappingProxyType(只读映射)。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze({"a": 1, "b": 2})
        assert isinstance(frozen, MappingProxyType)
        assert frozen["a"] == 1
        assert frozen["b"] == 2

    def test_deep_freeze_list_returns_tuple(self):
        """_deep_freeze(list) 返回 tuple(不可变序列)。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze([1, 2, 3])
        assert isinstance(frozen, tuple)
        assert frozen == (1, 2, 3)

    def test_deep_freeze_nested_dict_recursively(self):
        """_deep_freeze 递归冻结嵌套 dict(内层也是 MappingProxyType)。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze({"outer": {"inner": [1, {"x": 2}]}})
        assert isinstance(frozen, MappingProxyType)
        assert isinstance(frozen["outer"], MappingProxyType)
        assert isinstance(frozen["outer"]["inner"], tuple)
        assert isinstance(frozen["outer"]["inner"][1], MappingProxyType)
        assert frozen["outer"]["inner"][1]["x"] == 2

    def test_deep_freeze_scalar_unchanged(self):
        """_deep_freeze 标量(str/int/float/bool/None)保持不变。"""
        mod = _ensure_backup_dr_validate_importable()
        assert mod._deep_freeze("hello") == "hello"
        assert mod._deep_freeze(42) == 42
        assert mod._deep_freeze(3.14) == 3.14
        assert mod._deep_freeze(True) is True
        assert mod._deep_freeze(None) is None

    def test_deep_freeze_breaks_alias_reference(self):
        """R63 P0-02 核心防护: _deep_freeze 通过 deepcopy 断绝与调用方原 dict 的引用。

        构造后修改原 dict,冻结版本应不受影响(防别名引用篡改)。
        """
        mod = _ensure_backup_dr_validate_importable()
        original = {"a": 1, "nested": {"b": [1, 2]}}
        frozen = mod._deep_freeze(original)
        # 修改原 dict(模拟攻击者在验证后、写入前篡改)
        original["a"] = 999
        original["nested"]["b"].append(3)
        original["new_key"] = "evil"
        # 冻结版本应保持原值(deepcopy 断绝引用)
        assert frozen["a"] == 1
        assert list(frozen["nested"]["b"]) == [1, 2]
        assert "new_key" not in frozen

    def test_mapping_proxy_is_read_only(self):
        """MappingProxyType 不支持 __setitem__(TypeError)。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze({"a": 1})
        with pytest.raises(TypeError):
            frozen["a"] = 999  # type: ignore[index]

    def test_tuple_is_immutable(self):
        """tuple 不支持 __setitem__(TypeError)。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze([1, 2, 3])
        with pytest.raises(TypeError):
            frozen[0] = 999  # type: ignore[index]

    def test_to_serializable_converts_mapping_proxy_to_dict(self):
        """_to_serializable(MappingProxyType) 返回普通 dict(JSON 可序列化)。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze({"a": 1, "b": [2, {"c": 3}]})
        serializable = mod._to_serializable(frozen)
        assert isinstance(serializable, dict)
        assert isinstance(serializable["b"], list)
        assert isinstance(serializable["b"][1], dict)
        # 应可被 json.dumps 序列化(不依赖 default=str)
        json_str = json.dumps(serializable)
        assert json.loads(json_str) == {"a": 1, "b": [2, {"c": 3}]}

    def test_to_serializable_converts_tuple_to_list(self):
        """_to_serializable(tuple) 返回普通 list。"""
        mod = _ensure_backup_dr_validate_importable()
        frozen = mod._deep_freeze([1, {"a": [2, 3]}])
        serializable = mod._to_serializable(frozen)
        assert isinstance(serializable, list)
        assert isinstance(serializable[1], dict)
        assert isinstance(serializable[1]["a"], list)

    def test_compute_payload_digest_handles_frozen_structure(self):
        """_compute_payload_digest 对冻结结构(MappingProxyType/tuple)与普通 dict 产生相同 digest。

        这是 writer 端重算 digest 的基础 — 若冻结与未冻结结构 digest 不一致,
        重算将永远失败。
        """
        mod = _ensure_backup_dr_validate_importable()
        original = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        frozen = mod._deep_freeze(original)
        # 冻结结构与普通 dict 的 digest 必须一致
        digest_original = mod._compute_payload_digest(original)
        digest_frozen = mod._compute_payload_digest(frozen)
        assert digest_original == digest_frozen
        assert len(digest_frozen) == 64

    def test_compute_payload_digest_canonical_consistency(self):
        """_compute_payload_digest 使用 canonical JSON(sort_keys),key 顺序无关。"""
        mod = _ensure_backup_dr_validate_importable()
        d1 = {"b": 2, "a": 1, "tables": {"y": [], "x": [1]}}
        d2 = {"a": 1, "b": 2, "tables": {"x": [1], "y": []}}
        assert mod._compute_payload_digest(d1) == mod._compute_payload_digest(d2)
        # 冻结后也应一致
        assert mod._compute_payload_digest(mod._deep_freeze(d1)) == \
               mod._compute_payload_digest(mod._deep_freeze(d2))


# ═══════════════════════════════════════════════════════════════
# P0-02: VerifiedBackupPayload 深冻结
# ═══════════════════════════════════════════════════════════════


class TestVerifiedBackupPayloadDeepFreeze:
    """R63 P0-02: VerifiedBackupPayload 的 tables / payload 在 __post_init__ 中深冻结。"""

    def test_tables_is_mapping_proxy_after_construction(self):
        """VerifiedBackupPayload.tables 在构造后为 MappingProxyType(非普通 dict)。"""
        mod = _ensure_backup_dr_validate_importable()
        payload = _build_verified_payload(mod)
        assert isinstance(payload.tables, MappingProxyType)

    def test_payload_is_mapping_proxy_after_construction(self):
        """VerifiedBackupPayload.payload 在构造后为 MappingProxyType(非普通 dict)。"""
        mod = _ensure_backup_dr_validate_importable()
        payload = _build_verified_payload(mod)
        assert isinstance(payload.payload, MappingProxyType)

    def test_nested_tables_are_frozen(self):
        """VerifiedBackupPayload.tables 的嵌套 dict/list 递归冻结。"""
        mod = _ensure_backup_dr_validate_importable()
        tables = {"users": [{"user_id": 1, "meta": {"tags": ["a", "b"]}}]}
        payload = _build_verified_payload(mod, tables=tables)
        # 嵌套 list → tuple
        assert isinstance(payload.tables["users"], tuple)
        # 嵌套 dict → MappingProxyType
        assert isinstance(payload.tables["users"][0], MappingProxyType)
        assert isinstance(payload.tables["users"][0]["meta"], MappingProxyType)
        # list 内的 list → tuple
        assert isinstance(payload.tables["users"][0]["meta"]["tags"], tuple)

    def test_alias_reference_protection_original_dict_modified(self):
        """R63 P0-02 验收: 构造 VerifiedBackupPayload 后修改原始 dict,
        payload.tables 应不受影响(canonical bytes 已固定,property 重新解码即可)。"""
        mod = _ensure_backup_dr_validate_importable()
        original_tables = {"users": [{"user_id": 1}]}
        original_payload = {"tables": {"users": [{"user_id": 1}]}}
        # R64 P1-01: 仅传 canonical_payload_bytes,tables/payload 由 property 解码
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R63-P0-02-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(original_payload),
        )
        # 修改原 dict(模拟攻击者在验证后、写入前篡改)
        original_tables["users"].append({"user_id": 999, "evil": True})
        original_tables["injected"] = "evil"
        original_payload["tables"]["users"].append({"user_id": 888})
        original_payload["tampered"] = True
        # payload.tables 应保持原值(canonical bytes 不可变,property 每次重新解码)
        assert "injected" not in payload.tables
        assert len(payload.tables["users"]) == 1
        assert payload.tables["users"][0]["user_id"] == 1
        # payload.payload 也应保持原值
        assert "tampered" not in payload.payload
        assert len(payload.payload["tables"]["users"]) == 1

    def test_payload_digest_computed_from_frozen_payload(self):
        """payload_digest 从冻结后的 payload 计算,与 capability 绑定。"""
        mod = _ensure_backup_dr_validate_importable()
        original = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        payload = _build_verified_payload(mod, payload=original)
        # payload_digest 应等于对原始 dict 的 canonical digest
        # (因 _to_serializable 还原后 JSON 序列化与原始 dict 一致)
        expected = mod._compute_payload_digest(original)
        assert payload.payload_digest == expected
        assert len(payload.payload_digest) == 64

    def test_payload_digest_unchanged_after_original_dict_modified(self):
        """构造后修改原 dict,payload_digest 应保持不变(基于冻结后的 payload)。"""
        mod = _ensure_backup_dr_validate_importable()
        original = {"tables": {"users": [{"user_id": 1}]}}
        payload = _build_verified_payload(mod, payload=original)
        digest_before = payload.payload_digest
        # 修改原 dict
        original["tables"]["users"].append({"user_id": 999})
        original["tampered"] = True
        # payload_digest 应不变(基于冻结后的 payload,不受原 dict 影响)
        digest_after = payload.payload_digest
        assert digest_before == digest_after


# ═══════════════════════════════════════════════════════════════
# P0-02: Writer 端重算 digest(防御 object.__setattr__ 绕过 frozen)
# ═══════════════════════════════════════════════════════════════


class TestWriterDigestRecompute:
    """R63 P0-02: _restore_from_backup_data 首条语句重算 actual payload bytes digest,
    与 _capability.payload_digest 比对。防御 object.__setattr__ 绕过 frozen 替换 payload。"""

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_writer_fail_closed_on_object_setattr_tamper(self, monkeypatch):
        """R63 P0-02 核心验收: 构造 VerifiedBackupPayload 后用 object.__setattr__
        替换 payload,writer 端重算 digest 不匹配 capability → fail-closed (AppError)。

        攻击场景:
            1. 攻击者构造合法 VerifiedBackupPayload + _RestoreCapability
            2. 在调用 writer 前,用 object.__setattr__ 绕过 frozen=True
               替换 verified_payload.payload 为篡改后的 dict
            3. writer 首条语句重算 actual_payload_digest(基于篡改后的 payload)
            4. actual_payload_digest != _capability.payload_digest(构造时内嵌)
            5. raise AppError(fail-closed)
        """
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError, ErrorCodes
        mod = _ensure_backup_dr_validate_importable()

        # 1. 构造合法的 VerifiedBackupPayload + _RestoreCapability
        original_payload = {"tables": {"users": [{"user_id": 1, "username": "alice"}]}}
        verified_payload = mod.VerifiedBackupPayload(
            backup_id="backup_test_001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R63-P0-02-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(original_payload),
        )
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # 2. 模拟攻击:用 object.__setattr__ 绕过 frozen,替换 canonical_payload_bytes
        # R64 P1-01: payload/tables 是 property(从 canonical_payload_bytes 解码),
        # 攻击向量改为替换 canonical_payload_bytes(底层 bytes 字段)。
        tampered_payload = {"tables": {"users": [{"user_id": 999, "evil": True}]}}
        object.__setattr__(
            verified_payload,
            "canonical_payload_bytes",
            mod._canonical_json_bytes(tampered_payload),
        )

        # 3. writer 应在首条语句重算 digest → 不匹配 → AppError
        # mock _restore_crdb_tables 验证未被调用
        should_not_call = AsyncMock(
            return_value={"restored": {}, "skipped": [], "errors": []}
        )
        with patch("services.db_restore._restore_crdb_tables", should_not_call), \
             patch("services.db_restore._restore_sqlite_tables_to_db", should_not_call):
            with pytest.raises(AppError) as exc_info:
                await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        # writer 应在 digest 重算阶段就 fail-closed,不调用任何恢复函数
        assert not should_not_call.called, (
            "object.__setattr__ 篡改 payload 后,writer 应在 digest 重算阶段 "
            "fail-closed,不调用 _restore_crdb_tables / _restore_sqlite_tables_to_db"
        )

    @pytest.mark.asyncio
    async def test_writer_fail_closed_on_object_setattr_tamper_with_digest(self, monkeypatch):
        """R63 P0-02 附加: 即使攻击者同时用 object.__setattr__ 替换
        verified_payload.canonical_payload_bytes + payload_digest,writer 仍 fail-closed。

        writer 不使用 verified_payload.payload_digest,而是重算 actual bytes digest
        (sha256(canonical_payload_bytes))与 _capability.payload_digest 比对(不可伪造)。
        """
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError
        mod = _ensure_backup_dr_validate_importable()

        original_payload = {"tables": {"users": [{"user_id": 1}]}}
        verified_payload = mod.VerifiedBackupPayload(
            backup_id="backup_test_001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R63-P0-02-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(original_payload),
        )
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # 攻击者同时替换 canonical_payload_bytes 与 payload_digest(试图绕过)
        # R64 P1-01: 攻击向量改为替换 canonical_payload_bytes(底层 bytes 字段)
        tampered_payload = {"tables": {"evil": True}}
        tampered_canonical_bytes = mod._canonical_json_bytes(tampered_payload)
        import hashlib as _hashlib
        tampered_digest = _hashlib.sha256(tampered_canonical_bytes).hexdigest()
        object.__setattr__(
            verified_payload, "canonical_payload_bytes", tampered_canonical_bytes
        )
        object.__setattr__(verified_payload, "payload_digest", tampered_digest)

        # writer 重算 digest(基于 tampered canonical_payload_bytes)→ tampered_digest
        # 但 _capability.payload_digest 仍是原始值 → 不匹配 → AppError
        with patch("services.db_restore._restore_crdb_tables", AsyncMock()), \
             patch("services.db_restore._restore_sqlite_tables_to_db", AsyncMock()):
            with pytest.raises(AppError):
                await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )

    @pytest.mark.asyncio
    async def test_writer_succeeds_on_legitimate_payload(self, monkeypatch):
        """R63 P0-02 正向: 合法 VerifiedBackupPayload(未篡改)→ writer 重算 digest
        匹配 capability → assert_valid 通过 → 恢复成功。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        verified_payload = _build_verified_payload(mod)
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # mock 恢复函数(返回空结果,无错误)
        with patch("services.db_restore._restore_crdb_tables", AsyncMock()), \
             patch("services.db_restore._restore_sqlite_tables_to_db", AsyncMock()):
            result = await _restore_from_backup_data(
                verified_payload,
                _capability=cap,
                tables=None,
                merge=False,
            )

        # 应成功返回(无 AppError)
        assert "restored" in result
        assert "errors" in result
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_writer_uses_actual_payload_not_stored_digest(self, monkeypatch):
        """R63 P0-02: writer 重算 digest 传给 assert_valid(而非 verified_payload.payload_digest)。

        验证:即使 verified_payload.payload_digest 与 capability.payload_digest 不同,
        只要 actual bytes digest(重算值)与 capability.payload_digest 一致,
        writer 也应成功(不依赖 stored digest)。
        """
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        verified_payload = _build_verified_payload(mod)
        # 用 object.__setattr__ 故意把 stored payload_digest 设为错误值
        object.__setattr__(verified_payload, "payload_digest", "0" * 64)
        # capability 的 payload_digest 仍是正确的(从原始 payload 计算)
        cap = _build_valid_capability(
            mod,
            payload_digest=mod._compute_payload_digest(
                mod._to_serializable(verified_payload.payload)
            ),
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # writer 应重算 digest(与 capability 一致)→ 成功
        # (不使用 verified_payload.payload_digest = "0"*64)
        with patch("services.db_restore._restore_crdb_tables", AsyncMock()), \
             patch("services.db_restore._restore_sqlite_tables_to_db", AsyncMock()):
            result = await _restore_from_backup_data(
                verified_payload,
                _capability=cap,
                tables=None,
                merge=False,
            )

        assert result["errors"] == []


# ═══════════════════════════════════════════════════════════════
# P0-03: 恢复非跨数据源原子操作 — 任一失败 → AppError(fail-closed)
# ═══════════════════════════════════════════════════════════════


class TestFailClosedOnDataSourceFailure:
    """R63 P0-03: 任一数据源恢复失败 → 整个 operation FAILED(raise AppError,不返回部分成功)。"""

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_crdb_failure_raises_app_error(self, monkeypatch):
        """CRDB 恢复失败 → AppError(不返回部分成功)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError, ErrorCodes
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        verified_payload = _build_verified_payload(mod)
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # mock _restore_crdb_tables 抛出异常(模拟 CRDB 写入失败)
        async def _failing_crdb(tables_data, merge, result):
            result["errors"].append("CRDB connection refused")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        with patch("services.db_restore._restore_crdb_tables", _failing_crdb), \
             patch("services.db_restore._restore_sqlite_tables_to_db", AsyncMock()):
            with pytest.raises(AppError) as exc_info:
                await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_sqlite_failure_raises_app_error(self, monkeypatch):
        """SQLite 恢复失败 → AppError(不返回部分成功)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError, ErrorCodes
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        # 构造一个 SQLite source 的表(通过 mock get_table_source)
        verified_payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R63-P0-02-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(
                {"tables": {"sqlite_table": [{"col": 1}]}}
            ),
        )
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # mock get_table_source 返回 "sqlite" → 走 SQLite 恢复路径
        monkeypatch.setattr(
            "services.db_restore.get_table_source",
            lambda t: "sqlite" if t == "sqlite_table" else "crdb",
        )
        # mock BACKUP_SCHEMA 包含此表
        monkeypatch.setattr(
            "services.db_restore.BACKUP_SCHEMA",
            {"sqlite_table": MagicMock()},
        )

        # mock _restore_sqlite_tables_to_db 抛出异常
        async def _failing_sqlite(tables_data, merge, result, is_relay=False):
            result["errors"].append("SQLite disk I/O error")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        with patch("services.db_restore._restore_sqlite_tables_to_db", _failing_sqlite):
            with pytest.raises(AppError) as exc_info:
                await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_relay_sqlite_failure_raises_app_error(self, monkeypatch):
        """relay_sqlite 恢复失败 → AppError(不返回部分成功)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError, ErrorCodes
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        verified_payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R63-P0-02-test-fingerprint",
            canonical_payload_bytes=mod._canonical_json_bytes(
                {"tables": {"relay_table": [{"col": 1}]}}
            ),
        )
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        monkeypatch.setattr(
            "services.db_restore.get_table_source",
            lambda t: "relay_sqlite" if t == "relay_table" else "crdb",
        )
        monkeypatch.setattr(
            "services.db_restore.BACKUP_SCHEMA",
            {"relay_table": MagicMock()},
        )

        async def _failing_relay(tables_data, merge, result, is_relay=False):
            result["errors"].append("relay SQLite locked")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        with patch("services.db_restore._restore_sqlite_tables_to_db", _failing_relay):
            with pytest.raises(AppError) as exc_info:
                await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_errors_in_result_triggers_app_error(self, monkeypatch):
        """R63 P0-03 belt-and-suspenders: 即使恢复函数只 append error 不 raise,
        result["errors"] 非空 → 最终 AppError(fail-closed)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError, ErrorCodes
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        verified_payload = _build_verified_payload(mod)
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        # mock _restore_crdb_tables 只 append error,不 raise
        # (模拟旧代码行为 — 新代码会 raise,但 belt-and-suspenders 仍需兜底)
        async def _crdb_with_error(tables_data, merge, result):
            result["errors"].append("simulated partial failure")

        with patch("services.db_restore._restore_crdb_tables", _crdb_with_error), \
             patch("services.db_restore._restore_sqlite_tables_to_db", AsyncMock()):
            with pytest.raises(AppError) as exc_info:
                await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

    @pytest.mark.asyncio
    async def test_no_partial_success_returned(self, monkeypatch):
        """R63 P0-03: 失败时不返回 result dict(raise AppError,调用方无法获得部分成功)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data
        from services.error_codes import AppError, ErrorCodes
        mod = _ensure_backup_dr_validate_importable()

        # R63 P1-01: assert_valid 内部通过 get_cache_store() 消费 nonce,需注入真实 store
        await _patch_store(monkeypatch)

        verified_payload = _build_verified_payload(mod)
        cap = _build_valid_capability(
            mod,
            payload_digest=verified_payload.payload_digest,
            schema_fingerprint="R63-P0-02-test-fingerprint",
        )

        async def _failing_crdb(tables_data, merge, result):
            result["errors"].append("fail")
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        with patch("services.db_restore._restore_crdb_tables", _failing_crdb), \
             patch("services.db_restore._restore_sqlite_tables_to_db", AsyncMock()):
            # AppError 抛出 — 调用方无法获得 result dict(无部分成功)
            with pytest.raises(AppError):
                returned = await _restore_from_backup_data(
                    verified_payload,
                    _capability=cap,
                    tables=None,
                    merge=False,
                )
                # 若走到这里说明 fail-closed 未生效
                assert False, "应抛 AppError,不返回部分成功结果"


# ═══════════════════════════════════════════════════════════════
# P0-06: CLI 恢复路径与三段式备份发现模型一致
# ═══════════════════════════════════════════════════════════════


class TestRunRestoreThreeStageDiscovery:
    """R63 P0-06: run_restore 改为 backup_id/COMPLETE marker 发现路径。

    - 删除旧 get_latest_backup() 双重 loader
    - CLI 只接受 backup_id,由 strict service 自行解密 payload
    - data=None 传给 strict service(调用方不预加载)
    - 旧格式备份(无 COMPLETE marker)在 strict service 内 fail-closed
    """

    @pytest.mark.asyncio
    async def test_run_restore_requires_backup_id(self, monkeypatch):
        """R63 P0-06: run_restore 不传 backup_id → AppError(三段式发现入口缺失)。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

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
    async def test_run_restore_passes_data_none_to_strict_service(self, monkeypatch):
        """R63 P0-06: run_restore 传 data=None 给 validate_and_restore_backup_strict
        (由 strict service 自行 COMPLETE→manifest→payload 发现与解密,调用方不预加载)。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.backup_dr_validate import get_manifest_key

        # Mock R2 storage
        mock_r2 = MagicMock()
        mock_r2._access_key = "fake"
        mock_r2.configure = MagicMock(return_value=None)
        mock_r2.connect = AsyncMock(return_value=None)
        mock_r2.close = AsyncMock(return_value=None)
        monkeypatch.setattr(db_restore, "r2_storage", mock_r2)

        # Mock settings
        from config import settings as _settings
        monkeypatch.setattr(_settings, "R2_ACCOUNT_ID", "fake_account")
        monkeypatch.setattr(_settings, "R2_ACCESS_KEY_ID", "fake_ak")
        monkeypatch.setattr(_settings, "R2_SECRET_ACCESS_KEY", "fake_sk")
        monkeypatch.setattr(_settings, "R2_BUCKET_NAME", "fake_bucket")
        monkeypatch.setattr(_settings, "R2_ENDPOINT", "")
        monkeypatch.setattr(_settings, "BACKUP_SIGNING_KEY", b"fake_signing_key")
        monkeypatch.setattr(_settings, "BACKUP_KEK", "fake_kek_for_test")

        # Mock decryptor
        mock_decryptor = MagicMock()
        mock_decryptor.decrypt = lambda ct, aad=None: b'{"tables": {}}'
        monkeypatch.setattr(db_restore, "_build_cli_decryptor", lambda: mock_decryptor)

        # 捕获 validate_and_restore_backup_strict 的调用参数
        captured = {}

        async def _fake_strict(**kwargs):
            captured.update(kwargs)
            return {"restored": {}, "skipped": [], "errors": []}

        # Monkeypatch strict service(通过 sys.modules 注入)
        import services.backup_dr_validate as _bdv
        monkeypatch.setattr(_bdv, "validate_and_restore_backup_strict", _fake_strict)

        backup_id = "20260718_120000"
        await db_restore.run_restore(backup_id=backup_id, dry_run=False)

        # 验证:data=None(由 strict service 自行解密,调用方不预加载)
        assert captured.get("data") is None, (
            "run_restore 应传 data=None,由 strict service 自行解密 payload"
        )
        # 验证:backup_id 传给 strict service
        assert captured.get("timestamp") == backup_id
        assert captured.get("expected_backup_id") == backup_id
        # 验证:expected_manifest_key 由 backup_id 计算
        assert captured.get("expected_manifest_key") == get_manifest_key(backup_id, "full")

    @pytest.mark.asyncio
    async def test_run_restore_does_not_call_get_latest_backup(self, monkeypatch):
        """R63 P0-06: run_restore 不再调用 get_latest_backup(双重 loader 已删除)。

        get_latest_backup 使用 r2_storage.list_objects(prefix="db_backup/db_backup_")
        枚举旧格式单文件 — 与三段式模型不一致。新 run_restore 只下载特定 key
        (COMPLETE/manifest/payload),不枚举。
        """
        _ensure_restore_module_importable()
        from services import db_restore

        # Mock R2 storage — list_objects 不应被调用
        mock_r2 = MagicMock()
        mock_r2._access_key = "fake"
        mock_r2.configure = MagicMock(return_value=None)
        mock_r2.connect = AsyncMock(return_value=None)
        mock_r2.close = AsyncMock(return_value=None)
        mock_r2.list_objects = AsyncMock(
            side_effect=AssertionError("list_objects 不应被调用(双重 loader 已删除)")
        )
        mock_r2.download = AsyncMock(return_value=None)
        monkeypatch.setattr(db_restore, "r2_storage", mock_r2)

        from config import settings as _settings
        monkeypatch.setattr(_settings, "R2_ACCOUNT_ID", "fake_account")
        monkeypatch.setattr(_settings, "R2_ACCESS_KEY_ID", "fake_ak")
        monkeypatch.setattr(_settings, "R2_SECRET_ACCESS_KEY", "fake_sk")
        monkeypatch.setattr(_settings, "R2_BUCKET_NAME", "fake_bucket")
        monkeypatch.setattr(_settings, "R2_ENDPOINT", "")
        monkeypatch.setattr(_settings, "BACKUP_SIGNING_KEY", b"fake_signing_key")
        monkeypatch.setattr(_settings, "BACKUP_KEK", "fake_kek_for_test")

        mock_decryptor = MagicMock()
        mock_decryptor.decrypt = lambda ct, aad=None: b'{"tables": {}}'
        monkeypatch.setattr(db_restore, "_build_cli_decryptor", lambda: mock_decryptor)

        # get_latest_backup 不应被调用 — 若被调用则 AssertionError
        async def _should_not_call_get_latest():
            raise AssertionError("get_latest_backup 不应被调用(双重 loader 已删除)")
        monkeypatch.setattr(db_restore, "get_latest_backup", _should_not_call_get_latest)

        from services.error_codes import AppError
        # run_restore 会因 COMPLETE marker 缺失而 AppError(strict service fail-closed)
        # 但关键验证:list_objects 与 get_latest_backup 均未被调用
        with pytest.raises(AppError):
            await db_restore.run_restore(backup_id="20260718_120000", dry_run=True)

        # list_objects 不应被调用(双重 loader 已删除)
        assert not mock_r2.list_objects.called, (
            "run_restore 不应调用 r2_storage.list_objects(旧 get_latest_backup 的行为,"
            "与三段式发现模型不一致)"
        )

    @pytest.mark.asyncio
    async def test_run_restore_old_format_fails_with_migration_guidance(self, monkeypatch):
        """R63 P0-06: 旧格式备份(无 COMPLETE marker)→ AppError,
        日志指向离线导入/迁移工具。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes
        from loguru import logger as _loguru_logger

        captured_logs: list[str] = []
        _sink_id = _loguru_logger.add(
            lambda msg: captured_logs.append(msg.record["message"]),
            level="ERROR",
            format="{message}",
        )

        try:
            mock_r2 = MagicMock()
            mock_r2._access_key = "fake"
            mock_r2.configure = MagicMock(return_value=None)
            mock_r2.connect = AsyncMock(return_value=None)
            mock_r2.close = AsyncMock(return_value=None)
            # COMPLETE marker 不存在 → strict service fail-closed
            mock_r2.download = AsyncMock(return_value=None)
            monkeypatch.setattr(db_restore, "r2_storage", mock_r2)

            from config import settings as _settings
            monkeypatch.setattr(_settings, "R2_ACCOUNT_ID", "fake_account")
            monkeypatch.setattr(_settings, "R2_ACCESS_KEY_ID", "fake_ak")
            monkeypatch.setattr(_settings, "R2_SECRET_ACCESS_KEY", "fake_sk")
            monkeypatch.setattr(_settings, "R2_BUCKET_NAME", "fake_bucket")
            monkeypatch.setattr(_settings, "R2_ENDPOINT", "")
            monkeypatch.setattr(_settings, "BACKUP_SIGNING_KEY", b"fake_signing_key")
            monkeypatch.setattr(_settings, "BACKUP_KEK", "fake_kek_for_test")

            mock_decryptor = MagicMock()
            mock_decryptor.decrypt = lambda ct, aad=None: b'{"tables": {}}'
            monkeypatch.setattr(db_restore, "_build_cli_decryptor", lambda: mock_decryptor)

            with pytest.raises(AppError) as exc_info:
                await db_restore.run_restore(
                    backup_id="20260718_120000",
                    dry_run=True,
                )

            assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED

            log_text = "\n".join(captured_logs)
            assert (
                "迁移" in log_text or "migration" in log_text.lower() or "离线" in log_text
            ), f"日志应指向离线导入/迁移工具,实际: {log_text}"
        finally:
            _loguru_logger.remove(_sink_id)

    @pytest.mark.asyncio
    async def test_run_restore_missing_signing_key_fails(self, monkeypatch):
        """R63 P0-06: BACKUP_SIGNING_KEY 未配置 → AppError(无法验证 COMPLETE marker 签名)。"""
        _ensure_restore_module_importable()
        from services import db_restore
        from services.error_codes import AppError, ErrorCodes

        mock_r2 = MagicMock()
        mock_r2._access_key = "fake"
        mock_r2.configure = MagicMock(return_value=None)
        mock_r2.connect = AsyncMock(return_value=None)
        mock_r2.close = AsyncMock(return_value=None)
        monkeypatch.setattr(db_restore, "r2_storage", mock_r2)

        from config import settings as _settings
        monkeypatch.setattr(_settings, "R2_ACCOUNT_ID", "fake_account")
        monkeypatch.setattr(_settings, "R2_ACCESS_KEY_ID", "fake_ak")
        monkeypatch.setattr(_settings, "R2_SECRET_ACCESS_KEY", "fake_sk")
        monkeypatch.setattr(_settings, "R2_BUCKET_NAME", "fake_bucket")
        monkeypatch.setattr(_settings, "R2_ENDPOINT", "")
        # BACKUP_SIGNING_KEY 未配置(空)
        monkeypatch.setattr(_settings, "BACKUP_SIGNING_KEY", b"")

        with pytest.raises(AppError) as exc_info:
            await db_restore.run_restore(backup_id="20260718_120000", dry_run=True)

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED


# ═══════════════════════════════════════════════════════════════
# P1-04: cols 为空 → raise AppError(不 continue,不 BEGIN,无开放事务)
# ═══════════════════════════════════════════════════════════════


class TestNoOpenTransactionsOnColsEmpty:
    """R63 P1-04: cols 为空时 raise AppError(不 continue),
    确保不会在 BEGIN 后 continue 导致事务遗留。"""

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_cols_empty_raises_app_error_not_continue(self, monkeypatch):
        """R63 P1-04: validate_columns_for_table 返回空列表 → raise AppError(不 continue)。

        原实现: BEGIN 后若 cols 为空 continue,该分支无显式 COMMIT/ROLLBACK → 事务遗留。
        新实现: cols 校验在 BEGIN 前完成,空时 raise(不进入事务)。
        """
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables
        from services.error_codes import AppError, ErrorCodes

        # Mock db_client(连接已就绪,transaction 不应被调用)
        import database.session as session_mod
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.transaction = MagicMock(
            side_effect=AssertionError("transaction 不应在 cols 为空时被调用(无 BEGIN)")
        )
        monkeypatch.setattr(session_mod, "_client", mock_client)

        # Mock validate_columns_for_table 返回空列表
        monkeypatch.setattr(
            "services.db_restore.validate_columns_for_table",
            lambda table, cols: [],
        )

        result = {"restored": {}, "skipped": [], "errors": []}
        with pytest.raises(AppError) as exc_info:
            await _restore_crdb_tables(
                {"users": [{"user_id": 1, "username": "alice"}]},
                merge=False,
                result=result,
            )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        # 错误应被记录到 result["errors"]
        assert len(result["errors"]) > 0
        # transaction 不应被调用(无开放事务)
        assert not mock_client.transaction.called

    @pytest.mark.asyncio
    async def test_cols_empty_does_not_open_transaction(self, monkeypatch):
        """R63 P1-04: cols 为空时,db_client.transaction() 不被调用(不 BEGIN)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables

        import database.session as session_mod
        mock_client = MagicMock()
        mock_client.is_connected = True
        transaction_called = []

        @asynccontextmanager
        async def _fake_transaction():
            transaction_called.append(True)
            mock_conn = AsyncMock()
            yield mock_conn

        mock_client.transaction = _fake_transaction
        monkeypatch.setattr(session_mod, "_client", mock_client)

        monkeypatch.setattr(
            "services.db_restore.validate_columns_for_table",
            lambda table, cols: [],
        )

        result = {"restored": {}, "skipped": [], "errors": []}
        from services.error_codes import AppError
        with pytest.raises(AppError):
            await _restore_crdb_tables(
                {"users": [{"user_id": 1}]},
                merge=False,
                result=result,
            )

        # transaction 不应被调用(校验在 BEGIN 前)
        assert len(transaction_called) == 0, (
            "cols 为空时不应进入事务(避免 BEGIN 后 continue 遗留开放事务)"
        )

    @pytest.mark.asyncio
    async def test_column_validation_failure_raises_before_begin(self, monkeypatch):
        """R63 P1-04: validate_columns_for_table 抛 ValueError → raise AppError(不 BEGIN)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables
        from services.error_codes import AppError

        import database.session as session_mod
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.transaction = MagicMock(
            side_effect=AssertionError("transaction 不应在列校验失败时被调用")
        )
        monkeypatch.setattr(session_mod, "_client", mock_client)

        def _failing_validate(table, cols):
            raise ValueError(f"非法列名: {cols}")

        monkeypatch.setattr(
            "services.db_restore.validate_columns_for_table",
            _failing_validate,
        )

        result = {"restored": {}, "skipped": [], "errors": []}
        with pytest.raises(AppError):
            await _restore_crdb_tables(
                {"users": [{"bad_col": 1}]},
                merge=False,
                result=result,
            )

        assert not mock_client.transaction.called


# ═══════════════════════════════════════════════════════════════
# P1-04: 事务 context manager(异常自动 ROLLBACK,fail-closed)
# ═══════════════════════════════════════════════════════════════


class TestTransactionContextManager:
    """R63 P1-04 / P0-03: 使用 async with db_client.transaction() as conn context manager。

    - 事务由 context manager 自动管理(异常时自动 ROLLBACK)
    - 任一表写入异常 → raise AppError(fail-closed)
    - schema/column 校验全部在 BEGIN 前完成
    """

    def setup_method(self):
        """R63 P1-01: nonce 已持久化到 DB,无需进程内清理。"""
        _ensure_backup_dr_validate_importable()

    @pytest.mark.asyncio
    async def test_transaction_context_manager_is_used(self, monkeypatch):
        """R63 P1-04: _restore_crdb_tables 使用 async with db_client.transaction() as conn。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables

        import database.session as session_mod

        # 构造 async context manager mock
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=None)
        transaction_entered = []

        @asynccontextmanager
        async def _fake_transaction():
            transaction_entered.append(True)
            yield mock_conn

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.transaction = _fake_transaction
        monkeypatch.setattr(session_mod, "_client", mock_client)

        # Mock validate_columns_for_table 返回合法列
        monkeypatch.setattr(
            "services.db_restore.validate_columns_for_table",
            lambda table, cols: ["user_id", "username"],
        )

        result = {"restored": {}, "skipped": [], "errors": []}
        await _restore_crdb_tables(
            {"users": [{"user_id": 1, "username": "alice"}]},
            merge=False,
            result=result,
        )

        # 验证:事务 context manager 被调用
        assert len(transaction_entered) == 1, "应使用 db_client.transaction() context manager"
        # 验证:TRUNCATE 被调用(非 merge 模式)
        # 验证:INSERT 被调用
        assert mock_conn.execute.called
        # 验证:恢复成功(无错误)
        assert result["errors"] == []
        assert result["restored"]["users"] == 1

    @pytest.mark.asyncio
    async def test_exception_in_transaction_raises_app_error(self, monkeypatch):
        """R63 P1-04 / P0-03: 事务内异常 → context manager 自动 ROLLBACK → raise AppError。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables
        from services.error_codes import AppError, ErrorCodes

        import database.session as session_mod

        mock_conn = AsyncMock()
        # TRUNCATE 成功,INSERT 失败
        execute_calls = []

        async def _failing_execute(sql, *params):
            execute_calls.append(sql)
            if "INSERT" in sql:
                raise RuntimeError("CRDB unique constraint violation")
            return None

        mock_conn.execute = _failing_execute

        # 跟踪 context manager 的 __aexit__ 是否被调用(模拟 ROLLBACK)
        aexit_called = []

        @asynccontextmanager
        async def _fake_transaction():
            try:
                yield mock_conn
            finally:
                # 模拟 asyncpg conn.transaction() 的 __aexit__:异常时 ROLLBACK
                aexit_called.append(True)

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.transaction = _fake_transaction
        monkeypatch.setattr(session_mod, "_client", mock_client)

        monkeypatch.setattr(
            "services.db_restore.validate_columns_for_table",
            lambda table, cols: ["user_id"],
        )

        result = {"restored": {}, "skipped": [], "errors": []}
        with pytest.raises(AppError) as exc_info:
            await _restore_crdb_tables(
                {"users": [{"user_id": 1}]},
                merge=False,
                result=result,
            )

        assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        # context manager 的 __aexit__ 被调用(模拟 ROLLBACK)
        assert len(aexit_called) == 1, "事务 context manager __aexit__ 应被调用(自动 ROLLBACK)"
        # 错误被记录
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_validation_before_begin_no_open_transaction(self, monkeypatch):
        """R63 P1-04: schema/column 校验全部在 BEGIN 前完成 — 校验失败时不进入事务。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables
        from services.error_codes import AppError

        import database.session as session_mod

        mock_client = MagicMock()
        mock_client.is_connected = True
        transaction_called = []

        @asynccontextmanager
        async def _fake_transaction():
            transaction_called.append(True)
            yield AsyncMock()

        mock_client.transaction = _fake_transaction
        monkeypatch.setattr(session_mod, "_client", mock_client)

        # Mock:表名校验失败(模拟 _validate_identifier 抛异常)
        monkeypatch.setattr(
            "services.db_restore._validate_identifier" if hasattr(
                __import__("services.db_restore", fromlist=["_validate_identifier"]),
                "_validate_identifier"
            ) else "database.session._validate_identifier",
            lambda x: (_ for _ in ()).throw(ValueError(f"非法表名: {x}")),
        )

        result = {"restored": {}, "skipped": [], "errors": []}
        with pytest.raises(AppError):
            await _restore_crdb_tables(
                {"bad_table_name!": [{"col": 1}]},
                merge=False,
                result=result,
            )

        # 表名校验在 BEGIN 前 → transaction 不应被调用
        assert len(transaction_called) == 0, "表名校验失败不应进入事务(校验在 BEGIN 前)"

    @pytest.mark.asyncio
    async def test_empty_rows_skips_transaction(self, monkeypatch):
        """R63 P1-04: rows 为空时不进入事务(无记录不算错误,跳过)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_crdb_tables

        import database.session as session_mod

        mock_client = MagicMock()
        mock_client.is_connected = True
        transaction_called = []

        @asynccontextmanager
        async def _fake_transaction():
            transaction_called.append(True)
            yield AsyncMock()

        mock_client.transaction = _fake_transaction
        monkeypatch.setattr(session_mod, "_client", mock_client)

        result = {"restored": {}, "skipped": [], "errors": []}
        await _restore_crdb_tables(
            {"users": []},  # 空记录
            merge=False,
            result=result,
        )

        # 空记录不算错误,标记 0 行,不进入事务
        assert len(transaction_called) == 0
        assert result["restored"]["users"] == 0
        assert result["errors"] == []
