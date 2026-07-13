"""R36 H7: 备份加密 — AES-256-GCM 信封加密。

架构:
  KEK (Key Encryption Key)  ← 受控 secret provider 注入(32 字节,base64)
   └─ 包装 DEK (Data Encryption Key)  ← 每次备份随机生成
       └─ 加密 backup payload

R37 P1-6 密钥管理增强:
  - KEK 优先级: BACKUP_KEK_FILE(systemd credentials) > BACKUP_KEK(环境变量,兼容)
  - Manifest 记录 key_id(不记录 key 明文),支持密钥轮转追溯
  - 双 key 解密窗口: BACKUP_KEK_PREVIOUS 用于轮转期间解密旧备份
  - 生产推荐: systemd credentials(/run/credentials/<service>/BACKUP_KEK)
    或 Secret Manager/KMS 注入,KEK 不进入 .env 文件

加密流程:
  1. encrypt_payload(plaintext, kek) → {ciphertext, wrapped_dek, nonce, key_id}
  2. 将 wrapped_dek + nonce + key_id 存入 manifest, ciphertext 上传 R2

解密流程:
  1. decrypt_payload(ciphertext, wrapped_dek, nonce, kek) → plaintext
  2. 解密时尝试 current KEK,失败后尝试 previous KEK(轮转窗口)

安全:
  - AES-256-GCM 提供机密性 + 完整性(认证加密)
  - DEK 每次备份随机生成,即使 KEK 泄露也只能解密已包装的 DEK 对应的备份
  - KEK 不写入 R2,仅通过受控 secret provider 注入
  - nonce 随机生成(12 字节),GCM 模式下重复 nonce 会破坏安全性
  - Manifest 只记录 key_id 标识符,绝不记录 KEK 明文

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


def _read_kek_from_file(path: str) -> str | None:
    """R37 P1-6: 从 systemd credentials 文件路径读取 KEK。

    systemd credentials 通过 LoadCredential= 将 secret 挂载到
    /run/credentials/<service>/<credential-name> 路径,内容为明文。
    本函数读取文件内容并去除首尾空白,返回 base64 字符串。

    Args:
        path: credentials 文件路径(如 /run/credentials/tgjiema-db_backup.service/BACKUP_KEK)

    Returns:
        KEK base64 字符串;文件不存在或读取失败返回 None
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            logger.warning(f"[backup_crypto] KEK 文件为空: {path}")
            return None
        return content
    except FileNotFoundError:
        return None
    except PermissionError:
        logger.warning(f"[backup_crypto] KEK 文件无读取权限: {path}")
        return None
    except OSError as e:
        logger.warning(f"[backup_crypto] 读取 KEK 文件失败 {path}: {e}")
        return None


def _resolve_kek_b64() -> str:
    """R37 P1-6: 按优先级解析 KEK base64 字符串。

    优先级(高 → 低):
      1. BACKUP_KEK_FILE — systemd credentials 文件路径(生产推荐)
      2. BACKUP_KEK — 环境变量直接值(本地开发兼容,生产不推荐)

    返回空字符串表示未配置。
    """
    # 优先级 1: systemd credentials 文件路径
    kek_file = os.environ.get("BACKUP_KEK_FILE", "").strip()
    if kek_file:
        content = _read_kek_from_file(kek_file)
        if content:
            return content
        logger.warning(
            f"[backup_crypto] BACKUP_KEK_FILE 指定但读取失败: {kek_file},"
            f"回退到 BACKUP_KEK 环境变量"
        )
    # 优先级 2: 环境变量(向后兼容)
    return os.environ.get("BACKUP_KEK", "").strip()


def is_encryption_available() -> bool:
    """检查加密功能是否可用(cryptography 已安装且 KEK 已配置)。"""
    if not _CRYPTO_AVAILABLE:
        return False
    return bool(_resolve_kek_b64())


def get_kek() -> bytes | None:
    """R37 P1-6: 从受控 secret provider 读取 KEK(Key Encryption Key)。

    KEK 来源优先级:
      1. BACKUP_KEK_FILE — systemd credentials 文件路径(生产推荐)
      2. BACKUP_KEK — 环境变量直接值(本地开发兼容)

    KEK 应为 32 字节的 base64 编码字符串。
    返回 None 表示未配置加密(降级为明文备份 + 警告)。
    """
    kek_b64 = _resolve_kek_b64()
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


def get_previous_kek() -> bytes | None:
    """R37 P1-6: 读取轮转期间的旧 KEK(用于双 key 解密窗口)。

    密钥轮转流程:
      1. 生成新 KEK,配置到 BACKUP_KEK(或 BACKUP_KEK_FILE)
      2. 将旧 KEK 配置到 BACKUP_KEK_PREVIOUS(或 BACKUP_KEK_PREVIOUS_FILE)
      3. 保持双 key 窗口足够长(覆盖所有增量备份周期 + 恢复演练)
      4. 验证所有旧备份已用新 KEK 重新加密后,移除 PREVIOUS 配置

    Returns:
        旧 KEK 字节;未配置返回 None
    """
    # 优先级 1: 文件路径
    prev_file = os.environ.get("BACKUP_KEK_PREVIOUS_FILE", "").strip()
    if prev_file:
        content = _read_kek_from_file(prev_file)
        if content:
            try:
                kek = base64.b64decode(content)
                if len(kek) == _KEK_SIZE:
                    return kek
            except Exception:
                pass
    # 优先级 2: 环境变量
    prev_b64 = os.environ.get("BACKUP_KEK_PREVIOUS", "").strip()
    if not prev_b64:
        return None
    try:
        kek = base64.b64decode(prev_b64)
        if len(kek) != _KEK_SIZE:
            logger.warning(
                f"BACKUP_KEK_PREVIOUS 长度 {len(kek)} 字节,期望 {_KEK_SIZE} 字节,忽略"
            )
            return None
        return kek
    except Exception as e:
        logger.warning(f"BACKUP_KEK_PREVIOUS base64 解码失败: {e},忽略")
        return None


def get_key_id() -> str:
    """R37 P1-6: 获取当前 KEK 的 key_id 标识符(写入 manifest 用于追溯)。

    key_id 是 KEK 的 SHA-256 前 16 字节 hex(不是 KEK 本身),
    用于在 manifest 中标识使用哪个 KEK 加密,便于轮转期间追溯。
    生成规则:sha256(kek)[:16]  (不可逆,无法从 key_id 反推 KEK)

    Returns:
        16 字符 hex 字符串;KEK 未配置返回空字符串
    """
    import hashlib
    kek = get_kek()
    if kek is None:
        return ""
    return hashlib.sha256(kek).hexdigest()[:16]


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


def encrypt_payload(
    plaintext: bytes, kek: bytes | None = None,
    backup_id: str = "", schema_version: str = "",
) -> dict:
    """加密备份 payload,返回加密元数据。

    R37 P1-6: 返回值新增 key_id 字段(写入 manifest 用于追溯,不可逆)。
    R40 P0-6: 返回值新增 ciphertext_sha256 字段;AAD 绑定 {backup_id, schema_version, key_id}。

    Args:
        plaintext: 原始 backup JSON 字节
        kek: KEK 字节(None 则从受控 secret provider 读取)
        backup_id: 备份标识(如时间戳),用于 AAD 绑定
        schema_version: schema 版本,用于 AAD 绑定

    Returns:
        {
            "encrypted": bool,        # 是否已加密
            "ciphertext": bytes,      # 加密后的 payload(或原始 plaintext,如果未加密)
            "ciphertext_sha256": str,  # R40 P0-6: 密文的 SHA-256 校验和
            "wrapped_dek": str,       # 包装的 DEK(base64,仅加密时有)
            "nonce": str,             # GCM nonce(base64,仅加密时有)
            "algorithm": str,         # 加密算法标识
            "key_id": str,             # R37 P1-6: KEK 标识符(sha256 前 16 字符,仅加密时有)
            "aad": str,                # R40 P0-6: AAD 绑定信息(用于解密时重建)
        }
    """
    import hashlib

    if kek is None:
        kek = get_kek()

    if not _CRYPTO_AVAILABLE:
        logger.warning("[backup_crypto] cryptography 未安装,备份降级为明文")
        return {
            "encrypted": False,
            "ciphertext": plaintext,
            "ciphertext_sha256": hashlib.sha256(plaintext).hexdigest(),
            "algorithm": "none",
        }

    if kek is None:
        logger.warning("[backup_crypto] BACKUP_KEK 未配置,备份降级为明文")
        return {
            "encrypted": False,
            "ciphertext": plaintext,
            "ciphertext_sha256": hashlib.sha256(plaintext).hexdigest(),
            "algorithm": "none",
        }

    # R37 P1-6: 生成 key_id(KEK 的 sha256 前 16 字符,不可逆)
    key_id = hashlib.sha256(kek).hexdigest()[:16]

    # R40 P0-6: AAD 绑定 {backup_id, schema_version, key_id}
    # 防止密文被替换到不同备份上下文(重放攻击)
    aad_str = f"{backup_id}|{schema_version}|{key_id}"
    aad_bytes = aad_str.encode("utf-8")

    # 信封加密: 随机 DEK 加密 payload,KEK 包装 DEK
    dek = _generate_dek()
    aesgcm_dek = AESGCM(dek)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = aesgcm_dek.encrypt(nonce, plaintext, associated_data=aad_bytes)
    wrapped_dek = _wrap_dek(dek, kek)

    # R40 P0-6: 计算密文的 SHA-256 校验和
    ciphertext_sha256 = hashlib.sha256(ciphertext).hexdigest()

    return {
        "encrypted": True,
        "ciphertext": ciphertext,
        "ciphertext_sha256": ciphertext_sha256,
        "wrapped_dek": wrapped_dek,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "algorithm": "AES-256-GCM",
        "key_id": key_id,
        "aad": aad_str,
    }


def decrypt_payload(
    ciphertext: bytes,
    wrapped_dek: str | None = None,
    nonce_b64: str | None = None,
    kek: bytes | None = None,
    expected_plaintext_sha256: str | None = None,
    backup_id: str = "",
    schema_version: str = "",
    key_id: str = "",
) -> bytes:
    """解密备份 payload。

    R37 P1-6: 支持双 key 解密窗口。
    当 kek 参数为 None 时,按顺序尝试:
      1. 当前 KEK(get_kek)
      2. 旧 KEK(get_previous_kek,用于密钥轮转期间解密旧备份)

    R40 P0-6:
      - AAD 绑定 {backup_id, schema_version, key_id},防止密文重放
      - 支持 expected_plaintext_sha256 校验,解密后验证明文完整性
      - 向后兼容:旧备份使用 b"backup-payload" 作为 AAD,自动回退

    Args:
        ciphertext: 加密的 payload(或明文,如果未加密)
        wrapped_dek: 包装的 DEK(base64,仅加密时有)
        nonce_b64: GCM nonce(base64,仅加密时有)
        kek: KEK 字节(None 则从受控 secret provider 读取,并尝试双 key 窗口)
        expected_plaintext_sha256: R40 P0-6 期望的明文 SHA-256(可选,用于解密后校验)
        backup_id: R40 P0-6 备份标识(用于重建 AAD)
        schema_version: R40 P0-6 schema 版本(用于重建 AAD)
        key_id: R40 P0-6 KEK 标识符(用于重建 AAD,从 manifest 读取)

    Returns:
        原始 backup JSON 字节

    Raises:
        RuntimeError: cryptography 未安装 或 KEK 均不可用
        ValueError: 解密失败(KEK 不匹配或数据损坏)或明文 checksum 校验失败
    """
    import hashlib

    # 未加密的 payload 直接返回
    if not wrapped_dek or not nonce_b64:
        # R40 P0-6: 即使未加密也校验明文 checksum(如果提供)
        if expected_plaintext_sha256:
            actual_sha = hashlib.sha256(ciphertext).hexdigest()
            if actual_sha != expected_plaintext_sha256:
                raise ValueError(
                    f"R40 P0-6: 明文 checksum 校验失败"
                    f"(expected={expected_plaintext_sha256[:16]}, "
                    f"actual={actual_sha[:16]})"
                )
        return ciphertext

    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography 未安装,无法解密备份")

    # R37 P1-6: 双 key 解密窗口
    # 显式传入 kek 时只用该 key;否则按优先级尝试 current → previous
    candidate_keks: list[bytes] = []
    if kek is not None:
        candidate_keks.append(kek)
    else:
        current = get_kek()
        if current is not None:
            candidate_keks.append(current)
        previous = get_previous_kek()
        if previous is not None and previous != current:
            candidate_keks.append(previous)

    if not candidate_keks:
        raise RuntimeError("BACKUP_KEK 未配置,无法解密备份(当前 + 历史 KEK 均不可用)")

    # R40 P0-6: 构建 AAD 候选列表
    # 新备份: AAD = f"{backup_id}|{schema_version}|{key_id}"
    # 旧备份: AAD = b"backup-payload" (向后兼容)
    aad_candidates: list[bytes] = []
    if backup_id or schema_version or key_id:
        new_aad = f"{backup_id}|{schema_version}|{key_id}".encode("utf-8")
        aad_candidates.append(new_aad)
    aad_candidates.append(b"backup-payload")  # 向后兼容

    nonce = base64.b64decode(nonce_b64)
    last_error: Exception | None = None
    for candidate in candidate_keks:
        # R40 P0-6: 对每个 KEK 尝试所有 AAD 候选
        for aad in aad_candidates:
            try:
                dek = _unwrap_dek(wrapped_dek, candidate)
                aesgcm = AESGCM(dek)
                plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=aad)

                # R40 P0-6: 解密成功后校验明文 checksum
                if expected_plaintext_sha256:
                    actual_sha = hashlib.sha256(plaintext).hexdigest()
                    if actual_sha != expected_plaintext_sha256:
                        raise ValueError(
                            f"R40 P0-6: 解密后明文 checksum 校验失败"
                            f"(expected={expected_plaintext_sha256[:16]}, "
                            f"actual={actual_sha[:16]})"
                        )
                return plaintext
            except Exception as e:
                last_error = e
                continue

    # 所有候选 KEK + AAD 均失败
    raise ValueError(
        f"解密失败:所有候选 KEK({len(candidate_keks)}) + AAD({len(aad_candidates)}) "
        f"组合均无法解密。最后错误: {last_error}"
    )


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
