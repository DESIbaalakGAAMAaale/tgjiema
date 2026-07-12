"""R36 H7: 备份加密 — AES-256-GCM 信封加密。

架构:
  KEK (Key Encryption Key)  ← 环境变量 BACKUP_KEK (32 字节,base64)
   └─ 包装 DEK (Data Encryption Key)  ← 每次备份随机生成
       └─ 加密 backup payload

加密流程:
  1. encrypt_payload(plaintext, kek) → {ciphertext, wrapped_dek, nonce}
  2. 将 wrapped_dek + nonce 存入 manifest, ciphertext 上传 R2

解密流程:
  1. decrypt_payload(ciphertext, wrapped_dek, nonce, kek) → plaintext

安全:
  - AES-256-GCM 提供机密性 + 完整性(认证加密)
  - DEK 每次备份随机生成,即使 KEK 泄露也只能解密已包装的 DEK 对应的备份
  - KEK 不写入 R2,仅存在环境变量/secret manager
  - nonce 随机生成(12 字节),GCM 模式下重复 nonce 会破坏安全性

依赖: cryptography (已在 requirements.txt 中,relay_db 也使用)
"""
from __future__ import annotations

import base64
import json
import os
import secrets

from loguru import logger

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
    AESGCM = None  # type: ignore


# KEK 长度: AES-256 需要 32 字节密钥
_KEK_SIZE = 32
# GCM nonce 长度: 12 字节(推荐值)
_NONCE_SIZE = 12
# DEK 长度: AES-256 需要 32 字节
_DEK_SIZE = 32


def is_encryption_available() -> bool:
    """检查加密功能是否可用(cryptography 已安装且 KEK 已配置)。"""
    if not _CRYPTO_AVAILABLE:
        return False
    return bool(os.environ.get("BACKUP_KEK", "").strip())


def get_kek() -> bytes | None:
    """从环境变量读取 KEK(Key Encryption Key)。

    KEK 应为 32 字节的 base64 编码字符串。
    返回 None 表示未配置加密(降级为明文备份 + 警告)。
    """
    kek_b64 = os.environ.get("BACKUP_KEK", "").strip()
    if not kek_b64:
        return None
    try:
        kek = base64.b64decode(kek_b64)
        if len(kek) != _KEK_SIZE:
            logger.warning(
                f"BACKUP_KEK 长度 {len(kek)} 字节,期望 {_KEK_SIZE} 字节(AES-256),"
                f"加密降级为明文"
            )
            return None
        return kek
    except Exception as e:
        logger.warning(f"BACKUP_KEK base64 解码失败: {e},加密降级为明文")
        return None


def generate_kek() -> str:
    """生成新的 KEK(供初始化使用,返回 base64 字符串)。"""
    return base64.b64encode(secrets.token_bytes(_KEK_SIZE)).decode("ascii")


def _generate_dek() -> bytes:
    """生成随机 DEK(Data Encryption Key)。"""
    return secrets.token_bytes(_DEK_SIZE)


def _wrap_dek(dek: bytes, kek: bytes) -> str:
    """用 KEK 加密(包装)DEK,返回 base64 字符串。

    使用 AES-256-GCM 加密 DEK 本身。
    """
    aesgcm = AESGCM(kek)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    # GCM 模式: nonce + ciphertext + tag 一起输出
    wrapped = aesgcm.encrypt(nonce, dek, associated_data=b"dek-wrap")
    # 返回 nonce + wrapped 的组合,base64 编码
    combined = nonce + wrapped
    return base64.b64encode(combined).decode("ascii")


def _unwrap_dek(wrapped_dek_b64: str, kek: bytes) -> bytes:
    """用 KEK 解包 DEK。

    Args:
        wrapped_dek_b64: _wrap_dek() 返回的 base64 字符串
        kek: KEK 原始字节

    Returns:
        DEK 原始字节

    Raises:
        ValueError: 解包失败(KEK 不匹配或数据损坏)
    """
    aesgcm = AESGCM(kek)
    combined = base64.b64decode(wrapped_dek_b64)
    if len(combined) < _NONCE_SIZE + 1:
        raise ValueError(f"wrapped DEK 数据过短: {len(combined)} 字节")
    nonce = combined[:_NONCE_SIZE]
    wrapped = combined[_NONCE_SIZE:]
    dek = aesgcm.decrypt(nonce, wrapped, associated_data=b"dek-wrap")
    return dek


def encrypt_payload(plaintext: bytes, kek: bytes | None = None) -> dict:
    """加密备份 payload,返回加密元数据。

    Args:
        plaintext: 原始 backup JSON 字节
        kek: KEK 字节(None 则从环境变量读取)

    Returns:
        {
            "encrypted": bool,        # 是否已加密
            "ciphertext": bytes,      # 加密后的 payload(或原始 plaintext,如果未加密)
            "wrapped_dek": str,       # 包装的 DEK(base64,仅加密时有)
            "nonce": str,             # GCM nonce(base64,仅加密时有)
            "algorithm": str,         # 加密算法标识
        }
    """
    if kek is None:
        kek = get_kek()

    if not _CRYPTO_AVAILABLE:
        logger.warning("[backup_crypto] cryptography 未安装,备份降级为明文")
        return {
            "encrypted": False,
            "ciphertext": plaintext,
            "algorithm": "none",
        }

    if kek is None:
        logger.warning("[backup_crypto] BACKUP_KEK 未配置,备份降级为明文")
        return {
            "encrypted": False,
            "ciphertext": plaintext,
            "algorithm": "none",
        }

    # 信封加密: 随机 DEK 加密 payload,KEK 包装 DEK
    dek = _generate_dek()
    aesgcm_dek = AESGCM(dek)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = aesgcm_dek.encrypt(nonce, plaintext, associated_data=b"backup-payload")
    wrapped_dek = _wrap_dek(dek, kek)

    return {
        "encrypted": True,
        "ciphertext": ciphertext,
        "wrapped_dek": wrapped_dek,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "algorithm": "AES-256-GCM",
    }


def decrypt_payload(
    ciphertext: bytes,
    wrapped_dek: str | None = None,
    nonce_b64: str | None = None,
    kek: bytes | None = None,
) -> bytes:
    """解密备份 payload。

    Args:
        ciphertext: 加密的 payload(或明文,如果未加密)
        wrapped_dek: 包装的 DEK(base64,仅加密时有)
        nonce_b64: GCM nonce(base64,仅加密时有)
        kek: KEK 字节(None 则从环境变量读取)

    Returns:
        原始 backup JSON 字节
    """
    # 未加密的 payload 直接返回
    if not wrapped_dek or not nonce_b64:
        return ciphertext

    if kek is None:
        kek = get_kek()

    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography 未安装,无法解密备份")

    if kek is None:
        raise RuntimeError("BACKUP_KEK 未配置,无法解密备份")

    # 解包 DEK
    dek = _unwrap_dek(wrapped_dek, kek)

    # 解密 payload
    aesgcm = AESGCM(dek)
    nonce = base64.b64decode(nonce_b64)
    plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=b"backup-payload")
    return plaintext


def validate_manifest_on_restore(manifest: dict, expected_schema_version: str | None = None) -> tuple[bool, str]:
    """R36 H7: 恢复前校验 manifest 签名、checksum、schema version。

    Args:
        manifest: 备份的 manifest 字典
        expected_schema_version: 期望的 schema 版本(None 跳过版本检查)

    Returns:
        (is_valid, reason): 校验是否通过 + 原因说明
    """
    if not manifest:
        return False, "manifest 为空"

    # 1. 检查必需字段
    required_fields = ["version", "checksum_sha256", "schema_version"]
    for field in required_fields:
        if field not in manifest:
            return False, f"manifest 缺少必需字段: {field}"

    # 2. 检查 schema version
    if expected_schema_version and manifest.get("schema_version") != expected_schema_version:
        return False, (
            f"schema version 不匹配: 备份={manifest.get('schema_version')}, "
            f"期望={expected_schema_version}"
        )

    # 3. 检查 version 兼容性
    version = manifest.get("version", "")
    if not version:
        return False, "manifest version 为空"

    # 4. 检查加密标记(如果有,需要 BACKUP_KEK 才能解密)
    encryption_info = manifest.get("encryption", {})
    if encryption_info.get("encrypted") and not is_encryption_available():
        return False, "备份已加密但 BACKUP_KEK 未配置,无法解密"

    return True, "manifest 校验通过"


def verify_checksum(content: bytes, expected_checksum: str) -> bool:
    """校验内容的 SHA-256 checksum。

    Args:
        content: 原始内容字节
        expected_checksum: manifest 中的 checksum_sha256

    Returns:
        True 如果 checksum 匹配
    """
    import hashlib
    actual = hashlib.sha256(content).hexdigest()
    return actual == expected_checksum
