"""R64 P1-01: deep freeze 改为单一 canonical bytes 来源。

审计背景(R64 终审报告 P1-01):
    VerifiedBackupPayload 同时保存 ``tables`` 与 ``payload`` 两个独立字段,
    存在语义分叉风险(调用方可使二者不一致)。``_compute_payload_digest``
    使用 ``json.dumps(..., default=str)``,会把 NaN/Infinity/bytes/自定义对象
    静默字符串化,fail-open。

整改:
    - 验证对象只保存 ``canonical_payload_bytes``、digest 和解析后的只读 view。
    - ``tables`` 必须从已验证的同一 bytes 解码,不接受调用方独立传入。
    - 禁止 ``default=str``;只允许 JSON schema 声明类型,NaN/Infinity/bytes/
      自定义对象全部 fail-closed。

测试覆盖:
    1. VerifiedBackupPayload 构造只接受 canonical_payload_bytes(不接受 tables/payload)
    2. payload / tables 为 property(从 canonical_payload_bytes 解码,返回只读 view)
    3. payload_digest = sha256(canonical_payload_bytes)
    4. tables 与 payload 从同一 bytes 解码(单一 canonical 来源,无语义分叉)
    5. _compute_payload_digest 拒绝 NaN/Infinity(allow_nan=False,fail-closed)
    6. _compute_payload_digest 拒绝 bytes(无 default=str,fail-closed)
    7. _compute_payload_digest 拒绝自定义对象(无 default=str,fail-closed)
    8. _canonical_json_bytes 与 _compute_payload_digest 一致性
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _ensure_backup_dr_validate_importable():
    """确保 services.backup_dr_validate 可导入(仅依赖 loguru / i18n)。"""
    if "services.backup_dr_validate" in sys.modules:
        return sys.modules["services.backup_dr_validate"]
    import importlib
    return importlib.import_module("services.backup_dr_validate")


def _canonical_bytes(mod, data: dict) -> bytes:
    """用模块的 _canonical_json_bytes 序列化为 canonical JSON bytes。"""
    return mod._canonical_json_bytes(data)


# ═══════════════════════════════════════════════════════════════
# 1. VerifiedBackupPayload 构造 API:只接受 canonical_payload_bytes
# ═══════════════════════════════════════════════════════════════


class TestVerifiedBackupPayloadConstructorAPI:
    """R64 P1-01: VerifiedBackupPayload 构造只接受 canonical_payload_bytes。"""

    def test_accepts_canonical_payload_bytes(self):
        """构造时接受 canonical_payload_bytes 参数。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        assert payload.canonical_payload_bytes == cb

    def test_rejects_tables_kwarg(self):
        """构造时不接受 tables= kwarg(已移除独立 tables 参数)。"""
        mod = _ensure_backup_dr_validate_importable()
        cb = _canonical_bytes(mod, {"tables": {}})
        with pytest.raises(TypeError):
            mod.VerifiedBackupPayload(
                backup_id="b001",
                manifest_sha256="a" * 64,
                plaintext_sha256="c" * 64,
                schema_fingerprint="R64-P1-01-fingerprint",
                canonical_payload_bytes=cb,
                tables={"users": []},  # 应被拒绝
            )

    def test_rejects_payload_kwarg(self):
        """构造时不接受 payload= kwarg(已移除独立 payload 参数)。"""
        mod = _ensure_backup_dr_validate_importable()
        cb = _canonical_bytes(mod, {"tables": {}})
        with pytest.raises(TypeError):
            mod.VerifiedBackupPayload(
                backup_id="b001",
                manifest_sha256="a" * 64,
                plaintext_sha256="c" * 64,
                schema_fingerprint="R64-P1-01-fingerprint",
                canonical_payload_bytes=cb,
                payload={"tables": {}},  # 应被拒绝
            )


# ═══════════════════════════════════════════════════════════════
# 2. payload / tables 为 property(从 canonical_payload_bytes 解码)
# ═══════════════════════════════════════════════════════════════


class TestPayloadTablesAreProperties:
    """R64 P1-01: payload / tables 是从 canonical_payload_bytes 解码的只读 view。"""

    def test_payload_is_mapping_proxy(self):
        """payload property 返回 MappingProxyType(只读 view)。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        assert isinstance(payload.payload, MappingProxyType)

    def test_tables_is_mapping_proxy(self):
        """tables property 返回 MappingProxyType(只读 view)。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}]}}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        assert isinstance(payload.tables, MappingProxyType)

    def test_payload_decoded_from_canonical_bytes(self):
        """payload 内容与 canonical_payload_bytes 解码一致。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1, "name": "alice"}]}, "backup_id": "b001"}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        # payload 从 canonical_payload_bytes 解码
        assert payload.payload["backup_id"] == "b001"
        assert payload.payload["tables"]["users"][0]["name"] == "alice"

    def test_tables_decoded_from_canonical_bytes(self):
        """tables 内容从 canonical_payload_bytes 解码(单一来源)。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}], "posts": [{"id": 10}]}}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        assert "users" in payload.tables
        assert "posts" in payload.tables
        assert payload.tables["users"][0]["user_id"] == 1
        assert payload.tables["posts"][0]["id"] == 10

    def test_tables_empty_when_no_tables_key(self):
        """canonical_payload_bytes 不含 tables 键时,tables 返回空 view。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"backup_id": "b001"}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        # tables 为空(但不抛异常)
        assert len(payload.tables) == 0


# ═══════════════════════════════════════════════════════════════
# 3. payload_digest = sha256(canonical_payload_bytes)
# ═══════════════════════════════════════════════════════════════


class TestPayloadDigestFromCanonicalBytes:
    """R64 P1-01: payload_digest 从 canonical_payload_bytes 计算(sha256)。"""

    def test_digest_equals_sha256_of_canonical_bytes(self):
        """payload_digest == sha256(canonical_payload_bytes).hexdigest()。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="R64-P1-01-fingerprint",
            canonical_payload_bytes=cb,
        )
        expected = hashlib.sha256(cb).hexdigest()
        assert payload.payload_digest == expected
        assert len(payload.payload_digest) == 64

    def test_digest_changes_with_different_bytes(self):
        """不同 canonical_payload_bytes 产生不同 digest。"""
        mod = _ensure_backup_dr_validate_importable()
        cb1 = _canonical_bytes(mod, {"tables": {"users": [{"user_id": 1}]}})
        cb2 = _canonical_bytes(mod, {"tables": {"users": [{"user_id": 2}]}})
        p1 = mod.VerifiedBackupPayload(
            backup_id="b1", manifest_sha256="a" * 64, plaintext_sha256="c" * 64,
            schema_fingerprint="fp", canonical_payload_bytes=cb1,
        )
        p2 = mod.VerifiedBackupPayload(
            backup_id="b2", manifest_sha256="a" * 64, plaintext_sha256="c" * 64,
            schema_fingerprint="fp", canonical_payload_bytes=cb2,
        )
        assert p1.payload_digest != p2.payload_digest

    def test_digest_stable_across_repeated_payload_access(self):
        """多次访问 payload property 不改变 digest(基于不可变 bytes)。"""
        mod = _ensure_backup_dr_validate_importable()
        cb = _canonical_bytes(mod, {"tables": {"users": [{"user_id": 1}]}})
        payload = mod.VerifiedBackupPayload(
            backup_id="b001", manifest_sha256="a" * 64, plaintext_sha256="c" * 64,
            schema_fingerprint="fp", canonical_payload_bytes=cb,
        )
        d1 = payload.payload_digest
        _ = payload.payload
        _ = payload.tables
        d2 = payload.payload_digest
        assert d1 == d2


# ═══════════════════════════════════════════════════════════════
# 4. 单一 canonical 来源:tables 与 payload 从同一 bytes 解码
# ═══════════════════════════════════════════════════════════════


class TestSingleCanonicalSource:
    """R64 P1-01: tables 与 payload 从同一 canonical_payload_bytes 解码,无语义分叉。"""

    def test_tables_consistent_with_payload(self):
        """payload.tables 与 tables property 一致(同一 bytes 解码)。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        cb = _canonical_bytes(mod, data)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001", manifest_sha256="a" * 64, plaintext_sha256="c" * 64,
            schema_fingerprint="fp", canonical_payload_bytes=cb,
        )
        # tables property == payload["tables"](同一来源)
        from services.backup_dr_validate import _to_serializable
        assert _to_serializable(payload.tables) == _to_serializable(payload.payload["tables"])

    def test_no_independent_tables_field(self):
        """VerifiedBackupPayload 无独立 tables dataclass 字段(tables 为 property)。"""
        mod = _ensure_backup_dr_validate_importable()
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(mod.VerifiedBackupPayload)}
        assert "tables" not in field_names, (
            "tables 不应为 dataclass 字段(应为 property,从 canonical_payload_bytes 解码)"
        )
        assert "payload" not in field_names, (
            "payload 不应为 dataclass 字段(应为 property,从 canonical_payload_bytes 解码)"
        )
        assert "canonical_payload_bytes" in field_names, (
            "canonical_payload_bytes 应为 dataclass 字段"
        )


# ═══════════════════════════════════════════════════════════════
# 5. _compute_payload_digest 拒绝 NaN/Infinity/bytes/自定义对象(fail-closed)
# ═══════════════════════════════════════════════════════════════


class TestComputePayloadDigestFailClosed:
    """R64 P1-01: _compute_payload_digest 移除 default=str,对非法类型 fail-closed。"""

    def test_rejects_nan(self):
        """NaN 被 reject(allow_nan=False,fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._compute_payload_digest({"value": float("nan")})

    def test_rejects_infinity(self):
        """Infinity 被 reject(allow_nan=False,fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._compute_payload_digest({"value": float("inf")})

    def test_rejects_negative_infinity(self):
        """-Infinity 被 reject(allow_nan=False,fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._compute_payload_digest({"value": float("-inf")})

    def test_rejects_bytes(self):
        """bytes 被 reject(无 default=str,fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._compute_payload_digest({"data": b"raw bytes"})

    def test_rejects_custom_object(self):
        """自定义对象被 reject(无 default=str,fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError

        class Custom:
            pass

        with pytest.raises(AppError):
            mod._compute_payload_digest({"obj": Custom()})

    def test_rejects_set(self):
        """set 被 reject(不可 JSON 序列化,fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._compute_payload_digest({"items": {1, 2, 3}})

    def test_accepts_valid_json_types(self):
        """合法 JSON 类型(str/int/float/bool/None/list/dict)正常序列化。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {
            "string": "hello",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, 3],
            "nested": {"a": "b"},
        }
        digest = mod._compute_payload_digest(data)
        assert len(digest) == 64

    def test_canonical_consistency_preserved(self):
        """canonical JSON(sort_keys)一致性保留:不同 key 顺序相同内容 → 相同 digest。"""
        mod = _ensure_backup_dr_validate_importable()
        d1 = {"b": 2, "a": 1, "tables": {"y": [], "x": [1]}}
        d2 = {"a": 1, "b": 2, "tables": {"x": [1], "y": []}}
        assert mod._compute_payload_digest(d1) == mod._compute_payload_digest(d2)

    def test_no_default_str_in_source(self):
        """_compute_payload_digest 源码不含 default=str(静态检查)。"""
        mod = _ensure_backup_dr_validate_importable()
        import inspect
        source = inspect.getsource(mod._compute_payload_digest)
        assert "default=str" not in source, (
            "_compute_payload_digest 禁止使用 default=str(fail-closed 要求)"
        )


# ═══════════════════════════════════════════════════════════════
# 6. _canonical_json_bytes 辅助函数
# ═══════════════════════════════════════════════════════════════


class TestCanonicalJsonBytes:
    """R64 P1-01: _canonical_json_bytes 序列化为 canonical JSON bytes(fail-closed)。"""

    def test_returns_bytes(self):
        """返回 bytes 类型。"""
        mod = _ensure_backup_dr_validate_importable()
        result = mod._canonical_json_bytes({"a": 1})
        assert isinstance(result, bytes)

    def test_canonical_form(self):
        """canonical 形式:sort_keys + compact separators。"""
        mod = _ensure_backup_dr_validate_importable()
        result = mod._canonical_json_bytes({"b": 2, "a": 1})
        assert result == b'{"a":1,"b":2}'

    def test_rejects_nan(self):
        """NaN 被 reject(fail-closed)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._canonical_json_bytes({"value": float("nan")})

    def test_rejects_bytes(self):
        """bytes 被 reject(无 default=str)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            mod._canonical_json_bytes({"data": b"raw"})

    def test_consistent_with_compute_payload_digest(self):
        """_canonical_json_bytes + sha256 == _compute_payload_digest。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {"tables": {"users": [{"user_id": 1}]}, "backup_id": "b001"}
        cb = mod._canonical_json_bytes(data)
        digest = hashlib.sha256(cb).hexdigest()
        assert digest == mod._compute_payload_digest(data)
