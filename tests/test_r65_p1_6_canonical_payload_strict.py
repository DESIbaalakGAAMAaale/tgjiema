"""R65 P1-06: Canonical payload 构造时强校验。

审计背景(R65 终审报告 P1-06):
    Canonical payload 仍需构造时强校验。当前 ``payload`` property 调用
    ``json.loads()``,但 ``__post_init__`` 主要计算 SHA。应在构造时一次性验证:
    bytes 类型、UTF-8、JSON object、无重复 key、schema、顶层 tables 类型、
    canonical round-trip bytes 完全相等。否则"任意 JSON bytes"可能被称为 canonical。

整改:
    - VerifiedBackupPayload.__post_init__ 在计算 SHA-256 之前,先执行 7 维构造时校验
    - 任一校验失败 raise AppError(BACKUP_PAYLOAD_CANONICAL_INVALID),不计算 SHA
    - 新增 BACKUP_PAYLOAD_CANONICAL_INVALID 错误码 + zh-CN/en-US locale 条目

7 维校验:
    1. canonical_payload_bytes 必须为 bytes(拒绝 str)
    2. UTF-8 可解码
    3. JSON object(拒绝 array/primitive/null)
    4. 任意层级无重复 key(object_pairs_hook 检测)
    5. schema: version/backup_id/created_at/tables 必填且类型正确
    6. tables 值必须为 list
    7. canonical round-trip bytes 完全相等(拒绝非规范输入)

测试覆盖:
    - Happy path: 合法 canonical bytes 通过校验
    - 拒绝 str 类型(必须为 bytes)
    - 拒绝非法 UTF-8 bytes
    - 拒绝 JSON array(非 object)
    - 拒绝 JSON primitive(string/int/null)
    - 拒绝顶层重复 key
    - 拒绝嵌套重复 key
    - 拒绝缺失 version / backup_id / created_at / tables 字段
    - 拒绝 version 非 int(字符串) / version < 1
    - 拒绝 tables 非 dict / tables 值非 list
    - 拒绝非 canonical bytes(多余空白 / 不同 key 顺序 / ensure_ascii=True)
    - 拒绝 allow_nan=True 输出(NaN/Infinity)
    - SHA 在校验通过后仍正确计算
    - SHA 在校验失败时不计算(构造失败抛 AppError)
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


def _canonicalize(obj: dict) -> bytes:
    """将 dict 序列化为 canonical JSON bytes(sort_keys + 紧凑 + ensure_ascii=False)。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _valid_payload_data() -> dict:
    """构造一份合法的 canonical payload dict(满足 7 维校验)。"""
    return {
        "version": 1,
        "backup_id": "b001",
        "created_at": "2024-01-01T00:00:00Z",
        "tables": {
            "users": [{"user_id": 1, "username": "alice"}],
            "posts": [{"id": 10, "title": "hello"}],
        },
    }


def _build(mod, data: dict):
    """构造 VerifiedBackupPayload(默认填充信任链元数据)。"""
    return mod.VerifiedBackupPayload(
        backup_id="b001",
        manifest_sha256="a" * 64,
        plaintext_sha256="c" * 64,
        schema_fingerprint="R65-P1-06-fingerprint",
        canonical_payload_bytes=_canonicalize(data),
    )


def _build_with_bytes(mod, raw_bytes):
    """用任意 bytes 构造 VerifiedBackupPayload(用于负向测试)。"""
    return mod.VerifiedBackupPayload(
        backup_id="b001",
        manifest_sha256="a" * 64,
        plaintext_sha256="c" * 64,
        schema_fingerprint="R65-P1-06-fingerprint",
        canonical_payload_bytes=raw_bytes,
    )


# ═══════════════════════════════════════════════════════════════
# 1. Happy path — 合法 canonical bytes 通过校验
# ═══════════════════════════════════════════════════════════════


class TestHappyPath:
    """R65 P1-06: 合法 canonical bytes 通过 7 维校验。"""

    def test_well_formed_canonical_bytes_pass(self):
        """合法 canonical bytes 构造成功,不抛异常。"""
        mod = _ensure_backup_dr_validate_importable()
        data = _valid_payload_data()
        payload = _build(mod, data)
        assert payload.backup_id == "b001"
        assert payload.canonical_payload_bytes == _canonicalize(data)

    def test_payload_property_decoded_correctly(self):
        """校验通过后,payload property 正确解码。"""
        mod = _ensure_backup_dr_validate_importable()
        data = _valid_payload_data()
        payload = _build(mod, data)
        assert isinstance(payload.payload, MappingProxyType)
        assert payload.payload["version"] == 1
        assert payload.payload["backup_id"] == "b001"
        assert payload.payload["created_at"] == "2024-01-01T00:00:00Z"
        assert payload.payload["tables"]["users"][0]["username"] == "alice"

    def test_tables_property_decoded_correctly(self):
        """校验通过后,tables property 正确解码。"""
        mod = _ensure_backup_dr_validate_importable()
        data = _valid_payload_data()
        payload = _build(mod, data)
        assert isinstance(payload.tables, MappingProxyType)
        assert "users" in payload.tables
        assert "posts" in payload.tables
        assert payload.tables["users"][0]["user_id"] == 1

    def test_empty_tables_dict_passes(self):
        """tables 为空 dict(无表)也通过校验。"""
        mod = _ensure_backup_dr_validate_importable()
        data = {
            "version": 1,
            "backup_id": "b001",
            "created_at": "2024-01-01T00:00:00Z",
            "tables": {},
        }
        payload = _build(mod, data)
        assert len(payload.tables) == 0

    def test_extra_fields_allowed(self):
        """schema 校验为"at minimum",允许额外字段。"""
        mod = _ensure_backup_dr_validate_importable()
        data = _valid_payload_data()
        data["extra_field"] = "allowed"
        data["backup_type"] = "full"
        payload = _build(mod, data)
        assert payload.payload["extra_field"] == "allowed"
        assert payload.payload["backup_type"] == "full"

    def test_unicode_content_passes(self):
        """ensure_ascii=False 下,Unicode 字符直接编码通过校验。"""
        mod = _ensure_backup_dr_validate_importable()
        data = _valid_payload_data()
        data["tables"]["users"][0]["username"] = "张三"
        data["tables"]["users"][0]["nickname"] = "alice🎉"
        payload = _build(mod, data)
        assert payload.payload["tables"]["users"][0]["username"] == "张三"
        assert payload.payload["tables"]["users"][0]["nickname"] == "alice🎉"


# ═══════════════════════════════════════════════════════════════
# 2. bytes 类型校验(拒绝 str)
# ═══════════════════════════════════════════════════════════════


class TestRejectsStrType:
    """R65 P1-06: canonical_payload_bytes 必须为 bytes,拒绝 str。"""

    def test_rejects_str_type(self):
        """str 类型被拒绝(强制调用方 encode)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes
        # str 而非 bytes
        str_payload = json.dumps(
            _valid_payload_data(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        with pytest.raises(AppError) as exc_info:
            _build_with_bytes(mod, str_payload)
        assert exc_info.value.code == ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID

    def test_rejects_int_type(self):
        """int 类型被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            _build_with_bytes(mod, 12345)

    def test_rejects_none_type(self):
        """None 类型被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            _build_with_bytes(mod, None)

    def test_rejects_list_type(self):
        """list 类型被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        with pytest.raises(AppError):
            _build_with_bytes(mod, [b"not", b"bytes"])


# ═══════════════════════════════════════════════════════════════
# 3. UTF-8 可解码校验
# ═══════════════════════════════════════════════════════════════


class TestRejectsInvalidUtf8:
    """R65 P1-06: canonical_payload_bytes 必须为合法 UTF-8。"""

    def test_rejects_invalid_utf8_bytes(self):
        """非法 UTF-8 bytes 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        # 0xff 0xfe 不是合法 UTF-8 序列
        invalid_utf8 = b'{"version":1}\xff\xfe'
        with pytest.raises(AppError):
            _build_with_bytes(mod, invalid_utf8)

    def test_rejects_lone_continuation_byte(self):
        """孤立的 UTF-8 续接字节被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        invalid_utf8 = b'{"version":1}\x80\x81'
        with pytest.raises(AppError):
            _build_with_bytes(mod, invalid_utf8)


# ═══════════════════════════════════════════════════════════════
# 4. JSON object 校验(拒绝 array/primitive/null)
# ═══════════════════════════════════════════════════════════════


class TestRejectsNonJsonObject:
    """R65 P1-06: canonical payload 必须为 JSON object。"""

    def test_rejects_json_array(self):
        """JSON array 被拒绝(非 object)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        array_bytes = b'[1, 2, 3]'
        with pytest.raises(AppError):
            _build_with_bytes(mod, array_bytes)

    def test_rejects_json_string_primitive(self):
        """JSON string primitive 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        str_bytes = b'"just a string"'
        with pytest.raises(AppError):
            _build_with_bytes(mod, str_bytes)

    def test_rejects_json_int_primitive(self):
        """JSON int primitive 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        int_bytes = b'42'
        with pytest.raises(AppError):
            _build_with_bytes(mod, int_bytes)

    def test_rejects_json_null(self):
        """JSON null 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        null_bytes = b'null'
        with pytest.raises(AppError):
            _build_with_bytes(mod, null_bytes)

    def test_rejects_json_bool(self):
        """JSON bool primitive 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        bool_bytes = b'true'
        with pytest.raises(AppError):
            _build_with_bytes(mod, bool_bytes)


# ═══════════════════════════════════════════════════════════════
# 5. 任意层级无重复 key 校验
# ═══════════════════════════════════════════════════════════════


class TestRejectsDuplicateKeys:
    """R65 P1-06: 任意层级含重复 key 被拒绝(object_pairs_hook 检测)。"""

    def test_rejects_duplicate_top_level_keys(self):
        """顶层重复 key 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        # 直接构造含重复 key 的 JSON 字符串(json.dumps 会去重,需手写)
        dup_json = (
            '{"backup_id":"b001","backup_id":"b002",'
            '"created_at":"2024-01-01T00:00:00Z",'
            '"tables":{},"version":1}'
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, dup_json)

    def test_rejects_duplicate_nested_keys(self):
        """嵌套对象含重复 key 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        # tables.users 内部含重复 key
        dup_json = (
            '{"backup_id":"b001",'
            '"created_at":"2024-01-01T00:00:00Z",'
            '"tables":{"users":[{"user_id":1,"user_id":2}]},'
            '"version":1}'
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, dup_json)

    def test_rejects_duplicate_keys_in_tables_dict(self):
        """tables dict 含重复 key 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        dup_json = (
            '{"backup_id":"b001",'
            '"created_at":"2024-01-01T00:00:00Z",'
            '"tables":{"users":[],"users":[{"user_id":1}]},'
            '"version":1}'
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, dup_json)


# ═══════════════════════════════════════════════════════════════
# 6. Schema 校验 — 必填字段缺失
# ═══════════════════════════════════════════════════════════════


class TestRejectsMissingSchemaFields:
    """R65 P1-06: 顶层缺少 version/backup_id/created_at/tables 任一字段被拒绝。"""

    def test_rejects_missing_version(self):
        """缺少 version 字段被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        del data["version"]
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_missing_backup_id(self):
        """缺少 backup_id 字段被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        del data["backup_id"]
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_missing_created_at(self):
        """缺少 created_at 字段被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        del data["created_at"]
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_missing_tables(self):
        """缺少 tables 字段被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        del data["tables"]
        with pytest.raises(AppError):
            _build(mod, data)


# ═══════════════════════════════════════════════════════════════
# 7. Schema 校验 — 字段类型错误
# ═══════════════════════════════════════════════════════════════


class TestRejectsWrongSchemaTypes:
    """R65 P1-06: version/backup_id/created_at/tables 类型错误被拒绝。"""

    def test_rejects_version_as_string(self):
        """version 为字符串(如 "1")被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["version"] = "1"
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_version_zero(self):
        """version < 1 (0) 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["version"] = 0
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_version_negative(self):
        """version < 1 (-1) 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["version"] = -1
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_version_bool(self):
        """version 为 bool (True/False,Python 中 bool 是 int 子类) 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["version"] = True
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_backup_id_empty_string(self):
        """backup_id 为空字符串被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["backup_id"] = ""
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_backup_id_int(self):
        """backup_id 为 int 被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["backup_id"] = 123
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_created_at_not_iso8601(self):
        """created_at 非 ISO 8601 字符串被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["created_at"] = "not a date"
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_created_at_empty_string(self):
        """created_at 为空字符串被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["created_at"] = ""
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_tables_as_list(self):
        """tables 为 list(非 dict)被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"] = [{"users": []}]
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_tables_as_string(self):
        """tables 为 string(非 dict)被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"] = "not a dict"
        with pytest.raises(AppError):
            _build(mod, data)


# ═══════════════════════════════════════════════════════════════
# 8. tables 值类型校验(必须为 list)
# ═══════════════════════════════════════════════════════════════


class TestRejectsTablesValuesNotList:
    """R65 P1-06: tables 中每个表名对应的值必须为 list。"""

    def test_rejects_tables_value_as_dict(self):
        """tables 值为 dict(非 list)被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"] = {"user_id": 1}
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_tables_value_as_string(self):
        """tables 值为 string(非 list)被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"] = "not a list"
        with pytest.raises(AppError):
            _build(mod, data)

    def test_rejects_tables_value_as_int(self):
        """tables 值为 int(非 list)被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"] = 42
        with pytest.raises(AppError):
            _build(mod, data)


# ═══════════════════════════════════════════════════════════════
# 9. Canonical round-trip 等价校验
# ═══════════════════════════════════════════════════════════════


class TestRejectsNonCanonicalBytes:
    """R65 P1-06: canonical round-trip bytes 不相等的输入被拒绝。"""

    def test_rejects_extra_whitespace(self):
        """含多余空白的 JSON 被拒绝(非紧凑)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        # 含空格的非紧凑 JSON
        non_canonical = (
            '{ "version": 1, "backup_id": "b001", '
            '"created_at": "2024-01-01T00:00:00Z", "tables": {} }'
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)

    def test_rejects_different_key_order(self):
        """key 顺序非 sort_keys 排序被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        # version 在 backup_id 之后(非字母序)
        non_canonical = (
            '{"backup_id":"b001","created_at":"2024-01-01T00:00:00Z",'
            '"tables":{},"version":1}'
        ).encode("utf-8")
        # 注:上面其实是 sort_keys 顺序;改为真正的非字母序
        non_canonical = (
            '{"version":1,"backup_id":"b001",'
            '"created_at":"2024-01-01T00:00:00Z","tables":{}}'
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)

    def test_rejects_pretty_printed(self):
        """pretty-printed JSON(含换行缩进)被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        non_canonical = json.dumps(
            _valid_payload_data(), sort_keys=True, indent=2,
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)

    def test_rejects_ensure_ascii_true(self):
        """ensure_ascii=True 序列化的 Unicode 字符被拒绝(非 canonical)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"][0]["username"] = "张三"
        # ensure_ascii=True 会把 "张三" 编码为 \uXXXX
        non_canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)

    def test_rejects_alt_separators(self):
        """使用非紧凑分隔符(如 ', ' / ': ')被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        non_canonical = json.dumps(
            _valid_payload_data(), sort_keys=True, separators=(", ", ": "),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)


# ═══════════════════════════════════════════════════════════════
# 10. NaN/Infinity 校验(canonical round-trip 失败)
# ═══════════════════════════════════════════════════════════════


class TestRejectsNaNInfinity:
    """R65 P1-06: allow_nan=True 输出(NaN/Infinity)在 round-trip 时被拒绝。"""

    def test_rejects_nan_value(self):
        """NaN 值被拒绝(canonical round-trip 用 allow_nan=False 会抛错)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        # 用 allow_nan=True 序列化 NaN(产生 "NaN" 文本)
        data = _valid_payload_data()
        data["tables"]["users"][0]["score"] = float("nan")
        non_canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=True,
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)

    def test_rejects_infinity_value(self):
        """Infinity 值被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"][0]["score"] = float("inf")
        non_canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=True,
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)

    def test_rejects_negative_infinity_value(self):
        """-Infinity 值被拒绝。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"][0]["score"] = float("-inf")
        non_canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=True,
        ).encode("utf-8")
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)


# ═══════════════════════════════════════════════════════════════
# 11. SHA 计算行为
# ═══════════════════════════════════════════════════════════════


class TestShaComputationBehavior:
    """R65 P1-06: SHA 在校验通过后计算,校验失败时不计算。"""

    def test_sha_computed_after_validation_passes(self):
        """校验通过后,SHA-256 正确计算(== sha256(canonical_payload_bytes))。"""
        mod = _ensure_backup_dr_validate_importable()
        data = _valid_payload_data()
        cb = _canonicalize(data)
        payload = _build(mod, data)
        expected = hashlib.sha256(cb).hexdigest()
        assert payload.payload_digest == expected
        assert len(payload.payload_digest) == 64

    def test_sha_not_computed_when_validation_fails(self):
        """校验失败时不计算 SHA — 构造直接抛 AppError,payload_digest 未设置。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes
        # 用 str 而非 bytes — 校验失败
        str_payload = json.dumps(
            _valid_payload_data(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        with pytest.raises(AppError) as exc_info:
            _build_with_bytes(mod, str_payload)
        # 错误码必须是 CANONICAL_INVALID(而非其他)
        assert exc_info.value.code == ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID
        # AppError 已在 __post_init__ 抛出,dataclass 实例未完成构造,
        # 因此 payload_digest 字段从未被赋值(校验在 SHA 计算之前)

    def test_sha_not_computed_when_schema_validation_fails(self):
        """schema 校验失败时不计算 SHA。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        del data["version"]
        with pytest.raises(AppError):
            _build(mod, data)

    def test_sha_not_computed_when_roundtrip_fails(self):
        """canonical round-trip 失败时不计算 SHA。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        non_canonical = (
            '{"version":1,"backup_id":"b001",'
            '"created_at":"2024-01-01T00:00:00Z","tables":{}}'
        ).encode("utf-8")  # key 顺序非 sort_keys
        with pytest.raises(AppError):
            _build_with_bytes(mod, non_canonical)


# ═══════════════════════════════════════════════════════════════
# 12. AppError 参数完整性(reason / field)
# ═══════════════════════════════════════════════════════════════


class TestAppErrorParams:
    """R65 P1-06: AppError 携带 reason / field 参数,便于诊断。"""

    def test_app_error_includes_reason_and_field(self):
        """AppError 的 params 含 reason 与 field(按 safe_params 过滤后)。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes
        data = _valid_payload_data()
        del data["version"]
        with pytest.raises(AppError) as exc_info:
            _build(mod, data)
        assert exc_info.value.code == ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID
        # safe_params 已声明 reason + field
        params = exc_info.value.params
        assert "reason" in params
        assert "field" in params
        assert params["field"] == "version"

    def test_app_error_field_for_tables_value(self):
        """tables 值非 list 时,field 标识具体表名。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError
        data = _valid_payload_data()
        data["tables"]["users"] = "not a list"
        with pytest.raises(AppError) as exc_info:
            _build(mod, data)
        params = exc_info.value.params
        assert "field" in params
        assert "users" in params["field"]


# ═══════════════════════════════════════════════════════════════
# 13. 错误码与 locale 注册
# ═══════════════════════════════════════════════════════════════


class TestErrorCodeRegistered:
    """R65 P1-06: BACKUP_PAYLOAD_CANONICAL_INVALID 错误码已正确注册。"""

    def test_error_code_constant_exists(self):
        """ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID 常量存在。"""
        from services.error_codes import ErrorCodes
        assert hasattr(ErrorCodes, "BACKUP_PAYLOAD_CANONICAL_INVALID")
        assert ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID == "BACKUP.PAYLOAD.CANONICAL_INVALID"

    def test_error_registry_has_definition(self):
        """ErrorRegistry 已注册 BACKUP_PAYLOAD_CANONICAL_INVALID。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        definition = ErrorRegistry.get(ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID)
        assert definition is not None
        assert definition.code == ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID
        assert definition.message_key == "errors.backup.payload.canonical_invalid"
        # safe_params 含 reason 与 field
        assert "reason" in definition.safe_params
        assert "field" in definition.safe_params

    def test_locale_entries_exist(self):
        """zh-CN.json 与 en-US.json 含 canonical_invalid locale 条目。"""
        import json as _json
        zh_cn_path = REPO_ROOT / "locales" / "zh-CN.json"
        en_us_path = REPO_ROOT / "locales" / "en-US.json"
        with open(zh_cn_path, "r", encoding="utf-8") as f:
            zh_cn = _json.load(f)
        with open(en_us_path, "r", encoding="utf-8") as f:
            en_us = _json.load(f)
        assert "backup.payload.canonical_invalid" in zh_cn["errors"]
        assert "backup.payload.canonical_invalid" in en_us["errors"]
        # zh-CN 文案含 reason 与 field 占位符
        assert "{reason}" in zh_cn["errors"]["backup.payload.canonical_invalid"]
        assert "{field}" in zh_cn["errors"]["backup.payload.canonical_invalid"]
        # en-US 文案含 reason 与 field 占位符
        assert "{reason}" in en_us["errors"]["backup.payload.canonical_invalid"]
        assert "{field}" in en_us["errors"]["backup.payload.canonical_invalid"]


# ═══════════════════════════════════════════════════════════════
# 14. _enrich_payload_data 辅助函数
# ═══════════════════════════════════════════════════════════════


class TestEnrichPayloadData:
    """R65 P1-06: _enrich_payload_data 补齐缺失的必填字段。"""

    def test_enriches_missing_fields(self):
        """data 缺 version/backup_id/created_at 时,补齐后能通过校验。"""
        mod = _ensure_backup_dr_validate_importable()
        # 模拟生产 backup_data:仅含 tables
        raw_data = {"tables": {"users": [{"user_id": 1}]}}
        enriched = mod._enrich_payload_data(
            raw_data, backup_id="b001",
            created_at="2024-01-01T00:00:00Z",
        )
        # 补齐后含所有必填字段
        assert enriched["version"] == 1
        assert enriched["backup_id"] == "b001"
        assert enriched["created_at"] == "2024-01-01T00:00:00Z"
        assert enriched["tables"] == raw_data["tables"]
        # 原始 data 未被修改
        assert "version" not in raw_data
        # 补齐后的 data 可构造 VerifiedBackupPayload(不抛异常)
        payload = mod.VerifiedBackupPayload(
            backup_id="b001",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="fp",
            canonical_payload_bytes=_canonicalize(enriched),
        )
        assert payload.payload["version"] == 1

    def test_preserves_existing_fields(self):
        """data 已含 version/backup_id/created_at 时,保留原值。"""
        mod = _ensure_backup_dr_validate_importable()
        raw_data = {
            "version": 2,
            "backup_id": "existing_id",
            "created_at": "2024-02-02T00:00:00Z",
            "tables": {},
        }
        enriched = mod._enrich_payload_data(
            raw_data, backup_id="b001",
            created_at="2024-01-01T00:00:00Z",
        )
        # 保留原值,不覆盖
        assert enriched["version"] == 2
        assert enriched["backup_id"] == "existing_id"
        assert enriched["created_at"] == "2024-02-02T00:00:00Z"

    def test_returns_non_dict_unchanged(self):
        """非 dict 输入原样返回(由 VerifiedBackupPayload 校验拒绝)。"""
        mod = _ensure_backup_dr_validate_importable()
        result = mod._enrich_payload_data(
            "not a dict", backup_id="b001",
            created_at="2024-01-01T00:00:00Z",
        )
        assert result == "not a dict"
