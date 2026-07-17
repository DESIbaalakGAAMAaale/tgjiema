"""R56 §8: 备份灾备与数据可信性 — 三段式备份 + 恢复前验证 + staging 原子切换。

测试覆盖:
1. 三段式 key 生成(payload.enc / manifest.json / COMPLETE)
2. COMPLETE 标记验证(存在/缺失/损坏)
3. manifest 字段完整性(必填字段缺失检测)
4. schema compatibility(版本匹配/主版本兼容/不兼容)
5. payload 校验(ciphertext_sha256 匹配/不匹配/缺失)
6. staging 原子切换(成功/失败/fallback)
7. 完整恢复流程编排(依次校验 → 失败短路)
8. 故障矩阵(CRDB 不可用/R2 403/payload 损坏/manifest 损坏)

报告 §8 要求:
    "SQLite/R2 备份采用 payload.enc → manifest.json → COMPLETE"
    "恢复前先验证签名、校验和、schema compatibility、对象完整性;
     恢复过程使用 staging 目录,验证后原子切换"
    "故障矩阵:断网、kill -9、磁盘满、Redis 不可用、CRDB 不可用、
     R2 403、KEK 轮换中断、备份对象缺失/损坏"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.backup_dr_validate import (  # type: ignore  # noqa: E402
    COMPLETE_SUFFIX,
    MANIFEST_SUFFIX,
    PAYLOAD_SUFFIX,
    REQUIRED_MANIFEST_FIELDS,
    BackupValidationResult,
    atomic_restore_to_staging,
    build_complete_marker,
    get_complete_key,
    get_manifest_key,
    get_payload_key,
    validate_backup_completeness,
    validate_backup_manifest,
    validate_backup_payload,
    validate_backup_for_restore,
    validate_schema_compatibility,
)


# ── R59 P0-04 测试辅助函数 ──────────────────────────────────────
# R59 P0-04: 强制参数,不再允许 fail-open — 测试需提供真实签名与解密器

def _compute_marker_signature(
    signing_key: bytes,
    backup_id: str,
    manifest_key: str,
    manifest_sha256: str,
    payload_sha256: str,
) -> str:
    """R59 P0-04 测试辅助:计算 COMPLETE marker 的 HMAC-SHA256 签名。

    签名格式与 services.backup_dr_validate.validate_backup_completeness
    中的验签逻辑保持一致:
        HMAC-SHA256(signing_key, f"{backup_id}:{manifest_key}:{manifest_sha}:{payload_sha}")
    """
    sign_payload = (
        f"{backup_id}:{manifest_key}:{manifest_sha256}:{payload_sha256}"
    ).encode("utf-8")
    return hmac.new(signing_key, sign_payload, hashlib.sha256).hexdigest()


def _make_decryptor(plaintext: bytes) -> MagicMock:
    """R59 P0-04 测试辅助:构建返回指定明文的 decryptor mock。

    decryptor 需提供 decrypt(ciphertext, aad=...) -> plaintext 方法,
    与 services.backup_crypto 中的 BackupDecryptor 协议一致。
    """
    decryptor = MagicMock()
    decryptor.decrypt = MagicMock(return_value=plaintext)
    return decryptor


# ════════════════════════════════════════════════════════════════
# 1. 三段式 key 生成
# ════════════════════════════════════════════════════════════════


class TestThreeStageKeys:
    """三段式备份 key 生成测试。"""

    def test_payload_key_uses_enc_suffix(self):
        """payload 应使用 .enc 后缀。"""
        key = get_payload_key("20260716_120000", "full")
        assert key.endswith(PAYLOAD_SUFFIX)
        assert "payload_20260716_120000_full" in key

    def test_manifest_key_uses_json_suffix(self):
        """manifest 应使用 .json 后缀。"""
        key = get_manifest_key("20260716_120000", "full")
        assert key.endswith(MANIFEST_SUFFIX)
        assert "manifest_20260716_120000_full" in key

    def test_complete_key_uses_complete_suffix(self):
        """COMPLETE 标记应使用 .COMPLETE 后缀(无扩展名,纯标记)。"""
        key = get_complete_key("20260716_120000", "full")
        assert key.endswith(COMPLETE_SUFFIX)
        assert "COMPLETE_20260716_120000_full" in key

    def test_three_keys_share_timestamp(self):
        """三段式 key 应共享 timestamp + backup_type。"""
        ts = "20260716_120000"
        bt = "incremental"
        p = get_payload_key(ts, bt)
        m = get_manifest_key(ts, bt)
        c = get_complete_key(ts, bt)
        assert ts in p and ts in m and ts in c
        assert bt in p and bt in m and bt in c

    def test_complete_marker_content(self):
        """COMPLETE 标记内容应含 backup_id + manifest_key + schema + R58 P0-3 签名绑定字段。"""
        content = build_complete_marker(
            "20260716",
            "db_backup/manifest_20260716_full.json",
            manifest_sha256="a" * 64,
            payload_key="db_backup/payload_20260716_full.enc",
            payload_sha256="b" * 64,
            signature="c" * 64,
        )
        marker = json.loads(content)
        assert marker["backup_id"] == "20260716"
        assert marker["manifest_key"] == "db_backup/manifest_20260716_full.json"
        # R58 P0-3: schema 升级为签名版本
        assert marker["schema"] == "R58-P0-3-signed-three-stage"
        assert "created_at" in marker
        # R58 P0-3: 强绑定字段
        assert marker["manifest_sha256"] == "a" * 64
        assert marker["payload_key"] == "db_backup/payload_20260716_full.enc"
        assert marker["payload_sha256"] == "b" * 64
        assert marker["signature"] == "c" * 64


# ════════════════════════════════════════════════════════════════
# 2. COMPLETE 标记验证
# ════════════════════════════════════════════════════════════════


class TestValidateCompleteness:
    """COMPLETE 标记验证测试。"""

    @pytest.mark.asyncio
    async def test_complete_marker_exists(self):
        """COMPLETE 标记存在且 R59 P0-04 强制参数完整时返回 valid=True。"""
        # R59 P0-04: 强制参数 — signing_key/expected_manifest_key/expected_backup_id 必填,
        # 且需提供真实 HMAC 签名(不再允许跳过验签)
        signing_key = b"test_signing_key_r59"
        backup_id = "20260716"
        manifest_key = "manifest_key"
        manifest_sha = "a" * 64
        payload_sha = "b" * 64
        sig = _compute_marker_signature(
            signing_key, backup_id, manifest_key, manifest_sha, payload_sha,
        )
        marker_content = build_complete_marker(
            backup_id,
            manifest_key,
            manifest_sha256=manifest_sha,
            payload_key="payload_key",
            payload_sha256=payload_sha,
            signature=sig,
        )
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=marker_content)
        # R59 P0-04: 强制参数,不再允许 fail-open — 验签必填
        result = await validate_backup_completeness(
            backup_id, "full", mock_r2,
            expected_manifest_key=manifest_key,
            signing_key=signing_key,
            expected_backup_id=backup_id,
        )
        assert result.valid is True
        assert result.backup_id == backup_id

    @pytest.mark.asyncio
    async def test_complete_marker_missing(self):
        """COMPLETE 标记缺失时返回 COMPLETE_MARKER_MISSING。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=None)
        # R59 P0-04: 强制参数必填(原为可选,现 fail-closed)
        result = await validate_backup_completeness(
            "20260716", "full", mock_r2,
            expected_manifest_key="manifest_key",
            signing_key=b"signing_key",
            expected_backup_id="20260716",
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.COMPLETE_MARKER_MISSING"

    @pytest.mark.asyncio
    async def test_complete_marker_download_exception(self):
        """下载异常时返回 COMPLETE_MARKER_MISSING(不抛异常)。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(side_effect=Exception("R2 403 Forbidden"))
        # R59 P0-04: 强制参数必填(原为可选,现 fail-closed)
        result = await validate_backup_completeness(
            "20260716", "full", mock_r2,
            expected_manifest_key="manifest_key",
            signing_key=b"signing_key",
            expected_backup_id="20260716",
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.COMPLETE_MARKER_MISSING"


# ════════════════════════════════════════════════════════════════
# 3. manifest 字段完整性
# ════════════════════════════════════════════════════════════════


class TestValidateManifest:
    """manifest 字段完整性验证测试。"""

    def _build_valid_manifest(self) -> dict:
        """构建一个完整的有效 manifest。"""
        # R58 P0-3: manifest.backup_id 必须与请求 timestamp 严格匹配
        # (旧版 "20260716_120000" 与 "20260716" 不匹配,会触发 MANIFEST_INVALID)
        return {
            "version": "3.0",
            "commit_sha": "abc123def456abcdef1234567890abcdef123456",  # R58 P0-3: 40 hex
            "schema_version": "3.0",
            "plaintext_sha256": "a" * 64,
            "ciphertext_sha256": "b" * 64,
            "backup_id": "20260716",
            "content_size_bytes": 1024,
            "backup_started_at": "2026-07-16T12:00:00Z",
            "backup_finished_at": "2026-07-16T12:01:00Z",
            "table_stats": {"users": {"row_count": 100, "source": "crdb"}},
            "backup_type": "full",
            "encryption": {"encrypted": True, "key_id": "kek_v1"},
        }

    @pytest.mark.asyncio
    async def test_valid_manifest(self):
        """完整 manifest 返回 valid=True。"""
        manifest = self._build_valid_manifest()
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=json.dumps(manifest).encode())
        result = await validate_backup_manifest("20260716", "full", mock_r2)
        assert result.valid is True
        assert result.schema_version == "3.0"
        assert result.ciphertext_sha256 == "b" * 64
        assert result.encryption_key_id == "kek_v1"

    @pytest.mark.asyncio
    async def test_manifest_missing(self):
        """manifest.json 不存在时返回 MANIFEST_MISSING。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=None)
        result = await validate_backup_manifest("20260716", "full", mock_r2)
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.MANIFEST_MISSING"

    @pytest.mark.asyncio
    async def test_manifest_missing_required_field(self):
        """manifest 缺少必填字段时返回 MANIFEST_INCOMPLETE。"""
        manifest = self._build_valid_manifest()
        del manifest["ciphertext_sha256"]  # 删除一个必填字段
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=json.dumps(manifest).encode())
        result = await validate_backup_manifest("20260716", "full", mock_r2)
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.MANIFEST_INCOMPLETE"
        assert "ciphertext_sha256" in result.error_message

    @pytest.mark.asyncio
    async def test_manifest_corrupt_json(self):
        """manifest JSON 损坏时返回 MANIFEST_INVALID。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=b"not a valid json {{{")
        result = await validate_backup_manifest("20260716", "full", mock_r2)
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.MANIFEST_INVALID"

    @pytest.mark.asyncio
    async def test_manifest_without_encryption_key_id(self):
        """R58 P0-3: manifest encryption.key_id 缺失时拒绝恢复(严格校验,不允许空 key_id)。"""
        manifest = self._build_valid_manifest()
        manifest["encryption"] = {"encrypted": False, "algorithm": "none"}
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=json.dumps(manifest).encode())
        result = await validate_backup_manifest("20260716", "full", mock_r2)
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.MANIFEST_INVALID"
        assert "encryption.key_id" in result.error_message

    def test_required_manifest_fields_complete(self):
        """REQUIRED_MANIFEST_FIELDS 应包含所有 §8 要求的字段。"""
        # 报告 §8: "manifest 绑定 ciphertext hash、schema version、KEK key id、覆盖范围、创建版本"
        assert "ciphertext_sha256" in REQUIRED_MANIFEST_FIELDS
        assert "schema_version" in REQUIRED_MANIFEST_FIELDS
        assert "encryption" in REQUIRED_MANIFEST_FIELDS
        assert "backup_id" in REQUIRED_MANIFEST_FIELDS
        assert "table_stats" in REQUIRED_MANIFEST_FIELDS  # 覆盖范围
        assert "commit_sha" in REQUIRED_MANIFEST_FIELDS  # 创建版本


# ════════════════════════════════════════════════════════════════
# 4. schema compatibility
# ════════════════════════════════════════════════════════════════


class TestSchemaCompatibility:
    """schema 兼容性检查测试。"""

    def test_exact_match_compatible(self):
        """版本完全匹配时兼容。"""
        ok, _ = validate_schema_compatibility("3.0", "3.0")
        assert ok is True

    def test_major_version_compatible(self):
        """主版本号相同视为兼容(如 3.0 vs 3.1)。"""
        ok, _ = validate_schema_compatibility("3.0", "3.1")
        assert ok is True

    def test_major_version_incompatible(self):
        """主版本号不同不兼容(如 3.0 vs 4.0)。"""
        ok, reason = validate_schema_compatibility("3.0", "4.0")
        assert ok is False
        assert "major version incompatible" in reason

    def test_empty_manifest_version(self):
        """manifest schema_version 为空时拒绝。"""
        ok, reason = validate_schema_compatibility("", "3.0")
        assert ok is False
        assert "empty" in reason

    def test_empty_current_version(self):
        """current schema_version 为空时拒绝。"""
        ok, _ = validate_schema_compatibility("3.0", "")
        assert ok is False


# ════════════════════════════════════════════════════════════════
# 5. payload 校验
# ════════════════════════════════════════════════════════════════


class TestValidatePayload:
    """payload 校验测试(ciphertext_sha256)。"""

    @pytest.mark.asyncio
    async def test_payload_valid(self):
        """payload 密文校验通过。"""
        ciphertext = b"encrypted_payload_content"
        expected_sha = hashlib.sha256(ciphertext).hexdigest()
        plaintext = b"decrypted_plaintext_content"
        pt_sha = hashlib.sha256(plaintext).hexdigest()
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=ciphertext)
        # R59 P0-04: schema_version/decryptor 必填(fail-closed),AAD 绑定 5 字段
        result = await validate_backup_payload(
            "20260716", "full", expected_sha, pt_sha, mock_r2,
            schema_version="3.0",
            decryptor=_make_decryptor(plaintext),
        )
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_payload_hash_mismatch(self):
        """payload 密文校验和不匹配时拒绝。"""
        ciphertext = b"encrypted_payload_content"
        wrong_sha = "0" * 64  # 错误的 sha256
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=ciphertext)
        # R59 P0-04: schema_version/decryptor 必填(此处不会到达解密阶段)
        result = await validate_backup_payload(
            "20260716", "full", wrong_sha, "plaintext_sha", mock_r2,
            schema_version="3.0",
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH"

    @pytest.mark.asyncio
    async def test_payload_missing(self):
        """payload.enc 不存在时拒绝。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=None)
        # R59 P0-04: schema_version/decryptor 必填(此处不会到达解密阶段)
        result = await validate_backup_payload(
            "20260716", "full", "a" * 64, "b" * 64, mock_r2,
            schema_version="3.0",
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.PAYLOAD_MISSING"

    @pytest.mark.asyncio
    async def test_payload_download_exception(self):
        """下载异常时拒绝(不抛异常)。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(side_effect=Exception("R2 403 Forbidden"))
        # R59 P0-04: schema_version/decryptor 必填(此处不会到达解密阶段)
        result = await validate_backup_payload(
            "20260716", "full", "a" * 64, "b" * 64, mock_r2,
            schema_version="3.0",
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.PAYLOAD_INVALID"


# ════════════════════════════════════════════════════════════════
# 6. staging 原子切换
# ════════════════════════════════════════════════════════════════


class TestAtomicRestoreToStaging:
    """staging 原子切换测试。"""

    def test_successful_atomic_switch(self, tmp_path):
        """正常原子切换成功。"""
        staging = tmp_path / "staging.json"
        final = tmp_path / "final.json"
        data = {"users": [{"id": 1}], "files": []}
        ok, msg = atomic_restore_to_staging(staging, final, data)
        assert ok is True
        assert final.exists()
        assert not staging.exists()  # staging 应被 rename 掉
        # 验证内容
        content = json.loads(final.read_text())
        assert content == data

    def test_switch_overwrites_existing_final(self, tmp_path):
        """final 已存在时应覆盖(Windows fallback)。"""
        staging = tmp_path / "staging.json"
        final = tmp_path / "final.json"
        # 预先写入旧内容
        final.write_text('{"old": true}')
        data = {"new": True}
        ok, _ = atomic_restore_to_staging(staging, final, data)
        assert ok is True
        content = json.loads(final.read_text())
        assert content == data

    def test_switch_creates_parent_dirs(self, tmp_path):
        """父目录不存在时自动创建。"""
        staging = tmp_path / "subdir1" / "staging.json"
        final = tmp_path / "subdir2" / "final.json"
        data = {"test": 1}
        ok, _ = atomic_restore_to_staging(staging, final, data)
        assert ok is True
        assert final.exists()

    def test_switch_failure_cleans_staging(self, tmp_path):
        """切换失败时清理 staging(避免残留)。"""
        # 用包含 NUL 字符的路径触发失败(NUL 在文件名中非法,所有平台)
        staging = "\x00invalid_staging.json"
        final = tmp_path / "final.json"
        data = {"test": 1}
        ok, msg = atomic_restore_to_staging(staging, final, data)
        assert ok is False
        assert "failed" in msg
        assert not final.exists()  # final 不应被写入


# ════════════════════════════════════════════════════════════════
# 7. 完整恢复流程编排
# ════════════════════════════════════════════════════════════════


class TestValidateBackupForRestore:
    """完整恢复前验证流程编排测试(短路逻辑)。"""

    @pytest.mark.asyncio
    async def test_all_validations_pass(self):
        """所有验证通过时返回 valid=True(R59 P0-04 强制参数完整)。"""
        # R59 P0-04: 强制参数 — signing_key/expected_manifest_key/expected_backup_id/decryptor 必填
        signing_key = b"test_signing_key_r59"
        backup_id = "20260716"
        manifest_key = "manifest_key"
        ciphertext = b"payload"
        cipher_sha = hashlib.sha256(ciphertext).hexdigest()
        manifest_sha = "a" * 64
        plaintext = b"decrypted_payload"
        pt_sha = hashlib.sha256(plaintext).hexdigest()
        # R59 P0-04: 计算真实 HMAC 签名(不再允许跳过验签)
        sig = _compute_marker_signature(
            signing_key, backup_id, manifest_key, manifest_sha, cipher_sha,
        )
        marker = build_complete_marker(
            backup_id,
            manifest_key,
            manifest_sha256=manifest_sha,
            payload_key="payload_key",
            payload_sha256=cipher_sha,
            signature=sig,
        )
        manifest = {
            "version": "3.0",
            # R58 P0-3: commit_sha 必须 40 hex
            "commit_sha": "abc123def456abcdef1234567890abcdef123456",
            "schema_version": "3.0",
            "plaintext_sha256": pt_sha,
            "ciphertext_sha256": cipher_sha,
            "backup_id": backup_id,  # R58 P0-3: 严格匹配 timestamp
            "content_size_bytes": 100,
            "backup_started_at": "2026-07-16T12:00:00Z",
            "backup_finished_at": "2026-07-16T12:01:00Z",
            "table_stats": {},
            "backup_type": "full",
            "encryption": {"encrypted": True, "key_id": "kek_v1"},
        }
        mock_r2 = AsyncMock()
        # 第一次下载 COMPLETE,第二次下载 manifest,第三次下载 payload
        mock_r2.download = AsyncMock(side_effect=[
            marker,  # COMPLETE
            json.dumps(manifest).encode(),  # manifest
            ciphertext,  # payload
        ])
        # R59 P0-04: 强制参数 — 4 个必填参数传入
        result = await validate_backup_for_restore(
            backup_id, "full", mock_r2, "3.0",
            expected_manifest_key=manifest_key,
            signing_key=signing_key,
            expected_backup_id=backup_id,
            decryptor=_make_decryptor(plaintext),
            key_id="kek_v1",
        )
        assert result.valid is True
        assert result.encryption_key_id == "kek_v1"

    @pytest.mark.asyncio
    async def test_short_circuit_on_missing_complete(self):
        """COMPLETE 缺失时立即返回,不继续验证。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=None)  # COMPLETE 不存在
        # R59 P0-04: 强制参数必填(原为可选,现 fail-closed)
        result = await validate_backup_for_restore(
            "20260716", "full", mock_r2, "3.0",
            expected_manifest_key="manifest_key",
            signing_key=b"signing_key",
            expected_backup_id="20260716",
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.COMPLETE_MARKER_MISSING"
        # 只调用了一次 download(COMPLETE)
        assert mock_r2.download.call_count == 1

    @pytest.mark.asyncio
    async def test_short_circuit_on_manifest_missing(self):
        """manifest 缺失时立即返回(不校验 payload)。

        R58 P0-3: COMPLETE marker 必须包含 manifest_sha256/payload_sha256 强绑定字段,
        否则视为 INVALID 而非 MISSING(防止伪造 COMPLETE 跳过 manifest 校验)。
        R59 P0-04: 强制参数 — 需提供真实签名才能通过 validate_backup_completeness。
        """
        signing_key = b"test_signing_key_r59"
        backup_id = "20260716"
        manifest_key = "manifest_key"
        manifest_sha = "a" * 64
        payload_sha = "b" * 64
        # R59 P0-04: 计算真实 HMAC 签名
        sig = _compute_marker_signature(
            signing_key, backup_id, manifest_key, manifest_sha, payload_sha,
        )
        marker = build_complete_marker(
            backup_id,
            manifest_key,
            manifest_sha256=manifest_sha,
            payload_key="payload_key",
            payload_sha256=payload_sha,
            signature=sig,
        )
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(side_effect=[
            marker,  # COMPLETE 存在且完整
            None,    # manifest 不存在
        ])
        # R59 P0-04: 强制参数必填
        result = await validate_backup_for_restore(
            backup_id, "full", mock_r2, "3.0",
            expected_manifest_key=manifest_key,
            signing_key=signing_key,
            expected_backup_id=backup_id,
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.MANIFEST_MISSING"

    @pytest.mark.asyncio
    async def test_short_circuit_on_schema_incompatible(self):
        """schema 不兼容时立即返回(不校验 payload)。

        R58 P0-3: COMPLETE marker 必须包含强绑定字段才能进入 manifest 校验阶段。
        R59 P0-04: 强制参数 — 需提供真实签名才能通过 validate_backup_completeness。
        """
        signing_key = b"test_signing_key_r59"
        backup_id = "20260716"
        manifest_key = "manifest_key"
        manifest_sha = "a" * 64
        payload_sha = "b" * 64
        # R59 P0-04: 计算真实 HMAC 签名
        sig = _compute_marker_signature(
            signing_key, backup_id, manifest_key, manifest_sha, payload_sha,
        )
        marker = build_complete_marker(
            backup_id,
            manifest_key,
            manifest_sha256=manifest_sha,
            payload_key="payload_key",
            payload_sha256=payload_sha,
            signature=sig,
        )
        manifest = {
            "version": "3.0",
            "schema_version": "2.0",  # 主版本不兼容
            "plaintext_sha256": "a" * 64,
            "ciphertext_sha256": "b" * 64,
            "backup_id": backup_id,  # R58 P0-3: 严格匹配 timestamp
            "encryption": {"encrypted": True, "key_id": "kek_v1"},
            "commit_sha": "abc123def456abcdef1234567890abcdef123456",  # R58 P0-3: 40 hex
            "content_size_bytes": 100,
            "backup_started_at": "2026-07-16T12:00:00Z",
            "backup_finished_at": "2026-07-16T12:01:00Z",
            "table_stats": {},
            "backup_type": "full",
        }
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(side_effect=[
            marker,  # COMPLETE
            json.dumps(manifest).encode(),  # manifest
        ])
        # R59 P0-04: 强制参数必填(此处不会到达 payload 校验阶段)
        result = await validate_backup_for_restore(
            backup_id, "full", mock_r2, "3.0",  # current=3.0, manifest=2.0
            expected_manifest_key=manifest_key,
            signing_key=signing_key,
            expected_backup_id=backup_id,
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.SCHEMA_INCOMPATIBLE"


# ════════════════════════════════════════════════════════════════
# 8. 故障矩阵测试(§8 要求)
# ════════════════════════════════════════════════════════════════


class TestFaultMatrix:
    """故障矩阵:验证各种故障场景下的恢复安全性。

    报告 §8:
        "故障矩阵:断网、kill -9、磁盘满、Redis 不可用、CRDB 不可用、
         R2 403、KEK 轮换中断、备份对象缺失/损坏"
    """

    @pytest.mark.asyncio
    async def test_r2_403_forbidden(self):
        """R2 403 时恢复失败(不降级执行)。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(side_effect=Exception("403 Forbidden"))
        # R59 P0-04: 强制参数必填(原为可选,现 fail-closed)
        result = await validate_backup_completeness(
            "20260716", "full", mock_r2,
            expected_manifest_key="manifest_key",
            signing_key=b"signing_key",
            expected_backup_id="20260716",
        )
        assert result.valid is False
        assert "403" in result.error_message or "Forbidden" in result.error_message

    @pytest.mark.asyncio
    async def test_payload_corrupted(self):
        """payload 在 R2 中损坏时(校验和不匹配)拒绝恢复。"""
        # manifest 声明 sha256 = "a"*64
        # 但实际 payload 的 sha256 不是 "a"*64
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=b"corrupted_payload")
        # R59 P0-04: schema_version/decryptor 必填(此处不会到达解密阶段)
        result = await validate_backup_payload(
            "20260716", "full", "a" * 64, "b" * 64, mock_r2,
            schema_version="3.0",
            decryptor=MagicMock(),
        )
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH"

    @pytest.mark.asyncio
    async def test_manifest_corrupted(self):
        """manifest JSON 损坏时拒绝恢复。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=b"{ broken json")
        result = await validate_backup_manifest("20260716", "full", mock_r2)
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.MANIFEST_INVALID"

    @pytest.mark.asyncio
    async def test_backup_object_missing(self):
        """备份对象缺失(payload/manifest/COMPLETE 任一缺失)拒绝恢复。"""
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=None)
        # COMPLETE 缺失(R59 P0-04: 强制参数必填)
        r1 = await validate_backup_completeness(
            "20260716", "full", mock_r2,
            expected_manifest_key="manifest_key",
            signing_key=b"signing_key",
            expected_backup_id="20260716",
        )
        assert r1.valid is False
        # manifest 缺失
        r2 = await validate_backup_manifest("20260716", "full", mock_r2)
        assert r2.valid is False
        # payload 缺失(R59 P0-04: schema_version/decryptor 必填)
        r3 = await validate_backup_payload(
            "20260716", "full", "a" * 64, "b" * 64, mock_r2,
            schema_version="3.0",
            decryptor=MagicMock(),
        )
        assert r3.valid is False

    @pytest.mark.asyncio
    async def test_no_silent_recovery_on_partial_backup(self):
        """部分备份(无 COMPLETE)不应被静默恢复。"""
        # 场景:备份写到一半中断(有 payload + manifest,但无 COMPLETE)
        # R59 P0-04: build_complete_marker 需提供全部强制参数(原 2 参数调用已废弃)
        marker = build_complete_marker(
            "20260716", "manifest_key",
            manifest_sha256="a" * 64,
            payload_key="payload_key",
            payload_sha256="b" * 64,
            signature="c" * 64,
        )
        manifest = {
            "version": "3.0",
            "schema_version": "3.0",
            "plaintext_sha256": "a" * 64,
            "ciphertext_sha256": "b" * 64,
            "backup_id": "20260716",
            "encryption": {"encrypted": True, "key_id": "kek_v1"},
            "commit_sha": "abc",
            "content_size_bytes": 100,
            "backup_started_at": "2026-07-16T12:00:00Z",
            "backup_finished_at": "2026-07-16T12:01:00Z",
            "table_stats": {},
            "backup_type": "full",
        }
        # 第一次下载返回 None(COMPLETE 缺失)
        mock_r2 = AsyncMock()
        mock_r2.download = AsyncMock(return_value=None)
        # R59 P0-04: 强制参数必填(原为可选,现 fail-closed)
        result = await validate_backup_for_restore(
            "20260716", "full", mock_r2, "3.0",
            expected_manifest_key="manifest_key",
            signing_key=b"signing_key",
            expected_backup_id="20260716",
            decryptor=MagicMock(),
        )
        # 应在 COMPLETE 检查阶段就拒绝,不读取 manifest/payload
        assert result.valid is False
        assert result.error_code == "BACKUP.RESTORE.COMPLETE_MARKER_MISSING"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
