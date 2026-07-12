"""R36 Batch 8 H7: 备份加密 + 增量 watermark 测试。

覆盖:
- H7-1: AES-256-GCM 信封加密/解密往返
- H7-2: 未配置 KEK 时降级为明文
- H7-3: manifest 校验(schema_version/checksum/encryption)
- H7-4: 增量 watermark 计算
- H7-5: db_backup.backup_all_tables 支持 watermark 参数
- H7-6: settings 默认值(BACKUP_KEK)
- H7-7: deploy_vps_per_bot.sh db_backup secrets 含 BACKUP_KEK
- H7-8: registry.py 含 BACKUP_KEK 注册项
"""
import base64
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ════════════════════════════════════════════════════════════════
# 1. backup_crypto 模块测试
# ════════════════════════════════════════════════════════════════

def _crypto_available() -> bool:
    """检查 cryptography 是否可用(backup_crypto 导入成功即代表可用)。"""
    try:
        from services import backup_crypto
        return backup_crypto._CRYPTO_AVAILABLE
    except Exception:
        return False


# 模块级跳过:cryptography 不可用时跳过加密相关测试
@pytest.mark.skipif(
    not _crypto_available(),
    reason="cryptography 不可用,跳过加密测试",
)
class TestBackupCrypto:
    """R36 H7: backup_crypto 加密/解密测试。"""

    def test_generate_kek_returns_base64_32_bytes(self):
        """generate_kek() 返回 base64 字符串,解码后为 32 字节。"""
        from services.backup_crypto import generate_kek
        kek_b64 = generate_kek()
        kek = base64.b64decode(kek_b64)
        assert len(kek) == 32, f"KEK 应为 32 字节,实际 {len(kek)}"

    def test_encrypt_decrypt_roundtrip(self):
        """加密 → 解密往返保持原始内容。"""
        from services.backup_crypto import (
            encrypt_payload, decrypt_payload, generate_kek,
        )
        kek_b64 = generate_kek()
        kek = base64.b64decode(kek_b64)
        plaintext = b'{"tables": {"users": []}, "backup_time": "2026-07-12T10:00:00Z"}'

        enc = encrypt_payload(plaintext, kek=kek)
        assert enc["encrypted"] is True
        assert enc["algorithm"] == "AES-256-GCM"
        assert enc["ciphertext"] != plaintext  # 密文与明文不同
        assert enc["wrapped_dek"]
        assert enc["nonce"]

        # 解密
        dec = decrypt_payload(
            enc["ciphertext"],
            wrapped_dek=enc["wrapped_dek"],
            nonce_b64=enc["nonce"],
            kek=kek,
        )
        assert dec == plaintext

    def test_encrypt_with_env_kek(self, monkeypatch):
        """通过环境变量 BACKUP_KEK 读取 KEK。"""
        from services.backup_crypto import (
            encrypt_payload, decrypt_payload, generate_kek,
        )
        kek_b64 = generate_kek()
        monkeypatch.setenv("BACKUP_KEK", kek_b64)

        plaintext = b"hello backup"
        enc = encrypt_payload(plaintext)  # kek=None → 从 env 读取
        assert enc["encrypted"] is True

        dec = decrypt_payload(
            enc["ciphertext"],
            wrapped_dek=enc["wrapped_dek"],
            nonce_b64=enc["nonce"],
        )
        assert dec == plaintext

    def test_decrypt_with_wrong_kek_raises(self):
        """用错误的 KEK 解密应抛异常。"""
        from services.backup_crypto import (
            encrypt_payload, decrypt_payload, generate_kek,
        )
        kek1 = base64.b64decode(generate_kek())
        kek2 = base64.b64decode(generate_kek())
        plaintext = b"secret data"

        enc = encrypt_payload(plaintext, kek=kek1)
        # 用 kek2 解密应失败
        with pytest.raises(Exception):
            decrypt_payload(
                enc["ciphertext"],
                wrapped_dek=enc["wrapped_dek"],
                nonce_b64=enc["nonce"],
                kek=kek2,
            )

    def test_encrypt_degrades_to_plaintext_without_kek(self, monkeypatch):
        """未配置 BACKUP_KEK 时降级为明文。"""
        monkeypatch.delenv("BACKUP_KEK", raising=False)
        from services.backup_crypto import encrypt_payload
        plaintext = b"unencrypted backup"
        result = encrypt_payload(plaintext)
        assert result["encrypted"] is False
        assert result["ciphertext"] == plaintext
        assert result["algorithm"] == "none"

    def test_encrypt_with_invalid_kek_length_degrades(self, monkeypatch):
        """KEK 长度不对时降级为明文。"""
        monkeypatch.setenv("BACKUP_KEK", base64.b64encode(b"too-short").decode())
        from services.backup_crypto import encrypt_payload
        result = encrypt_payload(b"test")
        assert result["encrypted"] is False

    def test_is_encryption_available_without_kek(self, monkeypatch):
        """未配置 BACKUP_KEK 时 is_encryption_available() 返回 False。"""
        monkeypatch.delenv("BACKUP_KEK", raising=False)
        from services.backup_crypto import is_encryption_available
        assert is_encryption_available() is False

    def test_is_encryption_available_with_kek(self, monkeypatch):
        """配置 BACKUP_KEK 时 is_encryption_available() 返回 True。"""
        from services.backup_crypto import generate_kek
        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        from services.backup_crypto import is_encryption_available
        assert is_encryption_available() is True


# ════════════════════════════════════════════════════════════════
# 2. manifest 校验测试
# ════════════════════════════════════════════════════════════════

class TestManifestValidation:
    """R36 H7: manifest 校验测试。"""

    def test_valid_manifest_passes(self):
        """完整的 manifest 校验通过。"""
        from services.backup_crypto import validate_manifest_on_restore
        manifest = {
            "version": "3.0",
            "checksum_sha256": "abc123",
            "schema_version": "r36_40tables",
        }
        is_valid, reason = validate_manifest_on_restore(manifest)
        assert is_valid is True

    def test_empty_manifest_fails(self):
        """空 manifest 校验失败。"""
        from services.backup_crypto import validate_manifest_on_restore
        is_valid, reason = validate_manifest_on_restore({})
        assert is_valid is False
        assert "为空" in reason

    def test_missing_required_field_fails(self):
        """缺少必需字段校验失败。"""
        from services.backup_crypto import validate_manifest_on_restore
        manifest = {
            "version": "3.0",
            # 缺少 checksum_sha256
            "schema_version": "r36",
        }
        is_valid, reason = validate_manifest_on_restore(manifest)
        assert is_valid is False
        assert "checksum_sha256" in reason

    def test_schema_version_mismatch_fails(self):
        """schema version 不匹配校验失败。"""
        from services.backup_crypto import validate_manifest_on_restore
        manifest = {
            "version": "3.0",
            "checksum_sha256": "abc",
            "schema_version": "r35",
        }
        is_valid, reason = validate_manifest_on_restore(manifest, expected_schema_version="r36")
        assert is_valid is False
        assert "不匹配" in reason

    def test_encrypted_manifest_without_kek_fails(self, monkeypatch):
        """加密备份但未配置 BACKUP_KEK 校验失败。"""
        monkeypatch.delenv("BACKUP_KEK", raising=False)
        from services.backup_crypto import validate_manifest_on_restore
        manifest = {
            "version": "3.0",
            "checksum_sha256": "abc",
            "schema_version": "r36",
            "encryption": {"encrypted": True, "algorithm": "AES-256-GCM"},
        }
        is_valid, reason = validate_manifest_on_restore(manifest)
        assert is_valid is False
        assert "BACKUP_KEK" in reason

    def test_checksum_verification(self):
        """verify_checksum 正确校验内容。"""
        from services.backup_crypto import verify_checksum
        content = b'{"test": true}'
        expected = hashlib.sha256(content).hexdigest()
        assert verify_checksum(content, expected) is True
        assert verify_checksum(content, "wrong") is False


# ════════════════════════════════════════════════════════════════
# 3. Settings 默认值测试
# ════════════════════════════════════════════════════════════════

class TestSettingsBackupKek:
    """R36 H7: settings BACKUP_KEK 默认值。"""

    def test_backup_kek_default_empty(self):
        """BACKUP_KEK 默认为空字符串。"""
        from config import settings
        # conftest.py mock 的 settings 已设置 BACKUP_KEK=""
        assert settings.BACKUP_KEK == ""

    def test_backup_kek_in_registry_source(self):
        """registry.py 源码包含 BACKUP_KEK 注册项(避免 config 被 mock 的问题)。"""
        registry_path = Path(__file__).parent.parent / "config" / "registry.py"
        content = registry_path.read_text(encoding="utf-8")
        assert '"BACKUP_KEK"' in content, "config/registry.py 应注册 BACKUP_KEK"
        assert "SensitivityLevel.SECRET" in content
        assert 'services=["db_backup"]' in content

    def test_settings_py_has_backup_kek(self):
        """settings.py 源码包含 BACKUP_KEK 定义。"""
        settings_path = Path(__file__).parent.parent / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "BACKUP_KEK" in content


# ════════════════════════════════════════════════════════════════
# 4. 部署脚本测试
# ════════════════════════════════════════════════════════════════

class TestDeployScriptBatch8:
    """R36 H7: 部署脚本包含 BACKUP_KEK 隔离。"""

    def test_db_backup_secrets_include_backup_kek(self):
        """db_backup secrets 列表包含 BACKUP_KEK。"""
        script_path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = script_path.read_text(encoding="utf-8")
        assert "BACKUP_KEK" in content, "deploy_vps_per_bot.sh 应包含 BACKUP_KEK"
        # 验证在 db_backup secrets 中
        assert "[db_backup]=" in content
        db_backup_line = [l for l in content.split("\n") if "[db_backup]=" in l][0]
        assert "BACKUP_KEK" in db_backup_line


# ════════════════════════════════════════════════════════════════
# 5. .env.example 测试
# ════════════════════════════════════════════════════════════════

class TestEnvExampleBatch8:
    """R36 H7: .env.example 包含 BACKUP_KEK。"""

    def test_env_example_has_backup_kek(self):
        """ .env.example 包含 BACKUP_KEK 配置。"""
        env_path = Path(__file__).parent.parent / ".env.example"
        content = env_path.read_text(encoding="utf-8")
        assert "BACKUP_KEK=" in content
        assert "generate_kek" in content  # 包含生成命令


# ════════════════════════════════════════════════════════════════
# 6. db_backup 模块测试(mock R2 + CRDB)
# ════════════════════════════════════════════════════════════════

# 检查 db_backup 是否可导入(database.session 使用 PEP 604,Python 3.9 不可用)
def _db_backup_importable() -> bool:
    try:
        import services.db_backup  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _db_backup_importable(),
    reason="services.db_backup 不可用(需要 Python 3.10+ 或 asyncpg)",
)
class TestDbBackupIncremental:
    """R36 H7: db_backup 增量备份测试。"""

    def test_backup_all_tables_accepts_watermark(self):
        """backup_all_tables 接受 watermark 参数。"""
        import inspect
        from services.db_backup import backup_all_tables
        sig = inspect.signature(backup_all_tables)
        assert "watermark" in sig.parameters
        assert "backup_type" in sig.parameters

    def test_build_bundle_manifest_v3(self):
        """_build_bundle_manifest 生成 v3 manifest(含 backup_type/watermark/encryption)。"""
        from services.db_backup import _build_bundle_manifest
        from datetime import datetime, timezone
        backup_data = {"tables": {"users": [{"user_id": 1}]}}
        content = b'{"users": [{"user_id": 1}]}'
        start = datetime.now(timezone.utc)
        end = datetime.now(timezone.utc)
        manifest = _build_bundle_manifest(
            backup_data, content, start, end,
            backup_type="incremental",
            watermark="2026-07-12T10:00:00Z",
            prev_watermark="2026-07-12T09:00:00Z",
        )
        assert manifest["version"] == "3.0"
        assert manifest["backup_type"] == "incremental"
        assert manifest["watermark"] == "2026-07-12T10:00:00Z"
        assert manifest["prev_watermark"] == "2026-07-12T09:00:00Z"
        assert "encryption" in manifest
        assert manifest["encryption"]["encrypted"] is False

    def test_watermark_constants(self):
        """R36 H7: watermark 常量正确。"""
        from services import db_backup
        assert db_backup._WATERMARK_KEY == "db_backup/watermark.json"
        assert db_backup._FULL_BACKUP_INTERVAL == 24

    def test_compute_watermark_returns_iso_string(self):
        """_compute_watermark 返回 ISO 格式时间戳。"""
        from services.db_backup import _compute_watermark
        from services.backup_schema import BACKUP_SCHEMA
        tables = {
            "users": [
                {"user_id": 1, "updated_at": "2026-07-12T10:00:00Z"},
                {"user_id": 2, "updated_at": "2026-07-12T11:00:00Z"},
            ],
        }
        import asyncio
        wm = asyncio.run(_compute_watermark(tables))
        assert wm == "2026-07-12T11:00:00Z"
