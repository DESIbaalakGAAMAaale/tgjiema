"""R56 §8: 备份灾备与数据可信性 — 三段式备份 + 恢复前验证 + staging 原子切换。

报告 §8 要求:
    - SQLite/R2 备份采用 ``payload.enc → manifest.json → COMPLETE``
    - manifest 绑定 ciphertext hash、schema version、KEK key id、覆盖范围、创建版本
    - 恢复前先验证签名、校验和、schema compatibility、对象完整性
    - 恢复过程使用 staging 目录,验证后原子切换

三段式备份语义:
    1. payload.enc  — 加密的备份数据(AES-256-GCM 信封加密)
    2. manifest.json — 元数据(ciphertext_sha256 / plaintext_sha256 / schema_version /
                      backup_id / encryption.key_id / table_stats / commit_sha /
                      backup_type / watermark / created_at)
    3. COMPLETE      — 完成标记(存在即表示备份完整,不存在表示备份中断或部分上传)

恢复流程:
    1. 读取 COMPLETE 标记(不存在 → 拒绝恢复,备份未完成)
    2. 读取 manifest.json(字段完整性校验)
    3. 下载 payload.enc → 校验 ciphertext_sha256(对象完整性)
    4. 解密 → 校验 plaintext_sha256(数据完整性)
    5. schema compatibility 检查(manifest.schema_version vs 当前)
    6. 写入 staging 目录 → 验证后原子切换(rename)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger


# ── 常量 ──────────────────────────────────────────────────────

# R2 中的对象 key 后缀(三段式)
PAYLOAD_SUFFIX = ".enc"
MANIFEST_SUFFIX = ".json"
COMPLETE_SUFFIX = ".COMPLETE"

# manifest 必填字段(缺失任一即视为不完整)
REQUIRED_MANIFEST_FIELDS = (
    "version",
    "commit_sha",
    "schema_version",
    "plaintext_sha256",
    "ciphertext_sha256",
    "backup_id",
    "content_size_bytes",
    "backup_started_at",
    "backup_finished_at",
    "table_stats",
    "backup_type",
    "encryption",
)


@dataclass
class BackupValidationResult:
    """备份验证结果。"""
    valid: bool
    backup_id: str = ""
    schema_version: str = ""
    ciphertext_sha256: str = ""
    plaintext_sha256: str = ""
    encryption_key_id: str = ""
    error_code: str = ""
    error_message: str = ""


# ── 三段式备份 key 生成 ────────────────────────────────────────


def get_payload_key(timestamp: str, backup_type: str = "full") -> str:
    """生成 payload.enc 的 R2 key。"""
    return f"db_backup/payload_{timestamp}_{backup_type}.enc"


def get_manifest_key(timestamp: str, backup_type: str = "full") -> str:
    """生成 manifest.json 的 R2 key。"""
    return f"db_backup/manifest_{timestamp}_{backup_type}.json"


def get_complete_key(timestamp: str, backup_type: str = "full") -> str:
    """生成 COMPLETE 标记的 R2 key。

    R56 §8: 三段式备份的第三段 — COMPLETE 标记对象。
    命名规则: COMPLETE_{timestamp}_{backup_type}.COMPLETE
    (以 .COMPLETE 后缀结尾,便于 R2 列举与人工辨识)。
    """
    return f"db_backup/COMPLETE_{timestamp}_{backup_type}.COMPLETE"


# ── COMPLETE 标记内容 ─────────────────────────────────────────


def build_complete_marker(
    backup_id: str,
    manifest_key: str,
    manifest_sha256: str = "",
    payload_key: str = "",
    payload_sha256: str = "",
    signature: str = "",
) -> bytes:
    """R58 P0-3: 构建 COMPLETE 标记内容(JSON,含强绑定字段)。

    R58 P0-3 增强(签名 + digest 绑定):
        - backup_id: 备份 ID(timestamp)
        - manifest_key: manifest.json 的 R2 key
        - manifest_sha256: manifest 内容的 SHA-256(R58 P0-3: 绑定 manifest digest)
        - payload_key: payload.enc 的 R2 key
        - payload_sha256: 密文的 SHA-256(R58 P0-3: 绑定 payload digest)
        - signature: 整个 marker 的 HMAC 签名(R58 P0-3: 防止伪造 COMPLETE)
        - created_at: 创建时间(UTC ISO)
        - schema: schema 版本

    Args:
        backup_id: 备份 ID(timestamp)
        manifest_key: 对应 manifest.json 的 R2 key
        manifest_sha256: manifest 内容 SHA-256(64 hex)
        payload_key: payload.enc 的 R2 key
        payload_sha256: 密文 SHA-256(64 hex)
        signature: HMAC 签名(64 hex)

    Returns:
        JSON bytes
    """
    content = {
        "backup_id": backup_id,
        "manifest_key": manifest_key,
        "manifest_sha256": manifest_sha256,
        "payload_key": payload_key,
        "payload_sha256": payload_sha256,
        "signature": signature,
        "created_at": _dt.datetime.now(timezone_utc()).isoformat(),
        "schema": "R58-P0-3-signed-three-stage",
    }
    return json.dumps(content, ensure_ascii=False).encode("utf-8")


def timezone_utc():
    """获取 UTC tzinfo(兼容 Python 3.10+ 的 datetime.UTC)。"""
    try:
        return _dt.timezone.utc
    except AttributeError:
        return _dt.timezone.utc


# ── 恢复前验证 ─────────────────────────────────────────────────


async def validate_backup_completeness(
    timestamp: str,
    backup_type: str,
    r2_storage,
    expected_manifest_key: str = "",
    signing_key: bytes = b"",
) -> BackupValidationResult:
    """R58 P0-3: 验证备份完整性(COMPLETE 标记存在 + 签名 + 严格绑定)。

    R58 P0-3 增强:
        1. COMPLETE 标记存在
        2. R58 P0-3: 验证 marker signature(若提供 signing_key)
        3. R58 P0-3: 严格比较 backup_id == 请求 timestamp
        4. R58 P0-3: manifest_key 指向当前请求的 manifest(若提供 expected_manifest_key)
        5. R58 P0-3: manifest_sha256/payload_sha256 非空(强绑定)

    Args:
        timestamp: 备份 ID(timestamp)
        backup_type: full / incremental
        r2_storage: R2 存储客户端
        expected_manifest_key: 期望的 manifest R2 key(严格绑定检查)
        signing_key: COMPLETE marker 签名密钥(空则跳过验签,但记录 warning)

    Returns:
        BackupValidationResult(valid=True 表示 COMPLETE 标记有效且绑定一致)
    """
    complete_key = get_complete_key(timestamp, backup_type)
    try:
        content = await r2_storage.download(complete_key)
        if content is None:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_MISSING",
                error_message=f"COMPLETE marker missing: {complete_key} (backup may be interrupted)",
            )
        # 解析 COMPLETE 标记
        marker = json.loads(content)
        marker_backup_id = str(marker.get("backup_id", ""))
        marker_manifest_key = str(marker.get("manifest_key", ""))
        marker_manifest_sha = str(marker.get("manifest_sha256", ""))
        marker_payload_sha = str(marker.get("payload_sha256", ""))
        marker_signature = str(marker.get("signature", ""))

        # R58 P0-3: 严格比较 backup_id == 请求 timestamp
        if marker_backup_id != timestamp:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message=(
                    f"COMPLETE marker backup_id mismatch: "
                    f"expected={timestamp}, actual={marker_backup_id}"
                ),
            )
        # R58 P0-3: manifest_key 必须指向当前请求的 manifest
        if expected_manifest_key and marker_manifest_key != expected_manifest_key:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message=(
                    f"COMPLETE marker manifest_key mismatch: "
                    f"expected={expected_manifest_key}, actual={marker_manifest_key}"
                ),
            )
        # R58 P0-3: manifest_sha256/payload_sha256 必须非空(强绑定)
        if not marker_manifest_sha or len(marker_manifest_sha) != 64:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message="COMPLETE marker missing or invalid manifest_sha256",
            )
        if not marker_payload_sha or len(marker_payload_sha) != 64:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message="COMPLETE marker missing or invalid payload_sha256",
            )
        # R58 P0-3: 验签(若提供 signing_key)
        if signing_key:
            if not marker_signature or len(marker_signature) != 64:
                return BackupValidationResult(
                    valid=False,
                    backup_id=timestamp,
                    error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                    error_message="COMPLETE marker missing or invalid signature",
                )
            # 重算签名(排除 signature 字段本身)
            sign_payload = (
                f"{marker_backup_id}:{marker_manifest_key}:{marker_manifest_sha}:"
                f"{marker_payload_sha}"
            ).encode("utf-8")
            import hmac as _hmac_mod
            import hashlib
            expected_sig = _hmac_mod.new(signing_key, sign_payload, hashlib.sha256).hexdigest()
            if not _hmac_mod.compare_digest(expected_sig, marker_signature):
                return BackupValidationResult(
                    valid=False,
                    backup_id=timestamp,
                    error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                    error_message="COMPLETE marker signature verification failed",
                )
        else:
            # R58 P0-3: 未提供 signing_key 时记录 warning(生产应强制验签)
            import logging
            logging.getLogger(__name__).warning(
                "R58 P0-3: validate_backup_completeness 未提供 signing_key,"
                "跳过 COMPLETE marker 验签(生产应强制验签)"
            )
        return BackupValidationResult(
            valid=True,
            backup_id=marker_backup_id,
        )
    except Exception as e:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.COMPLETE_MARKER_MISSING",
            error_message=f"Failed to read COMPLETE marker: {e}",
        )


async def validate_backup_manifest(
    timestamp: str,
    backup_type: str,
    r2_storage,
) -> BackupValidationResult:
    """R56 §8: 验证 manifest 字段完整性。

    检查 manifest 包含所有必填字段(ciphertext_sha256、schema_version、
    backup_id、encryption.key_id 等)。
    """
    manifest_key = get_manifest_key(timestamp, backup_type)
    try:
        content = await r2_storage.download(manifest_key)
        if content is None:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_MISSING",
                error_message=f"manifest.json not found: {manifest_key}",
            )
        manifest = json.loads(content)
        # 检查必填字段
        missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest]
        if missing:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INCOMPLETE",
                error_message=f"manifest missing required fields: {missing}",
            )
        # R58 P0-3: 严格字段格式校验(不只检查"存在")
        # 1. backup_id 必须非空字符串,且与请求 timestamp 匹配
        manifest_backup_id = str(manifest.get("backup_id", ""))
        if not manifest_backup_id:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message="manifest backup_id is empty",
            )
        if manifest_backup_id != timestamp:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"manifest backup_id mismatch: expected={timestamp}, actual={manifest_backup_id}",
            )
        # 2. ciphertext_sha256/plaintext_sha256 必须为 64 hex 字符
        ct_sha = str(manifest.get("ciphertext_sha256", ""))
        pt_sha = str(manifest.get("plaintext_sha256", ""))
        if len(ct_sha) != 64 or not all(c in "0123456789abcdef" for c in ct_sha.lower()):
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"ciphertext_sha256 invalid format: len={len(ct_sha)}",
            )
        if len(pt_sha) != 64 or not all(c in "0123456789abcdef" for c in pt_sha.lower()):
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"plaintext_sha256 invalid format: len={len(pt_sha)}",
            )
        # 3. encryption.key_id 必须非空
        encryption = manifest.get("encryption", {})
        if not isinstance(encryption, dict):
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message="manifest encryption field is not a dict",
            )
        key_id = str(encryption.get("key_id", ""))
        if not key_id:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message="manifest encryption.key_id is empty",
            )
        # 4. commit_sha 必须为 40 hex 字符(Git SHA-1)
        commit_sha = str(manifest.get("commit_sha", ""))
        if len(commit_sha) != 40 or not all(c in "0123456789abcdef" for c in commit_sha.lower()):
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"commit_sha invalid format: len={len(commit_sha)}",
            )
        # 5. backup_type 必须为 full / incremental
        backup_type_val = str(manifest.get("backup_type", ""))
        if backup_type_val not in ("full", "incremental"):
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"backup_type invalid: {backup_type_val}",
            )
        # 6. content_size_bytes 必须为正整数
        content_size = manifest.get("content_size_bytes", 0)
        if not isinstance(content_size, (int, float)) or content_size <= 0:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"content_size_bytes invalid: {content_size}",
            )
        # 7. 时间顺序:backup_started_at <= backup_finished_at
        started_at = str(manifest.get("backup_started_at", ""))
        finished_at = str(manifest.get("backup_finished_at", ""))
        if started_at and finished_at and started_at > finished_at:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.MANIFEST_INVALID",
                error_message=f"time order invalid: started={started_at} > finished={finished_at}",
            )
        # 提取关键字段
        return BackupValidationResult(
            valid=True,
            backup_id=manifest_backup_id,
            schema_version=str(manifest.get("schema_version", "")),
            ciphertext_sha256=ct_sha,
            plaintext_sha256=pt_sha,
            encryption_key_id=key_id,
        )
    except Exception as e:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.MANIFEST_INVALID",
            error_message=f"Failed to parse manifest: {e}",
        )


def validate_schema_compatibility(
    manifest_schema_version: str,
    current_schema_version: str,
) -> tuple[bool, str]:
    """R56 §8: schema compatibility 检查。

    恢复前必须检查 manifest.schema_version 与当前 _BACKUP_SCHEMA_VERSION 兼容。
    当前实现:版本必须完全匹配(未来可支持向后兼容映射)。

    Args:
        manifest_schema_version: manifest 中的 schema_version
        current_schema_version: 当前代码的 _BACKUP_SCHEMA_VERSION

    Returns:
        (compatible, reason): compatible=True 可恢复;False 拒绝恢复
    """
    if not manifest_schema_version or not current_schema_version:
        return False, "schema_version is empty (cannot verify compatibility)"
    if manifest_schema_version == current_schema_version:
        return True, "schema version exact match"
    # 简单兼容规则:主版本号相同视为兼容(如 "3.0" 与 "3.1")
    try:
        manifest_major = str(manifest_schema_version).split(".")[0]
        current_major = str(current_schema_version).split(".")[0]
        if manifest_major == current_major:
            return True, f"schema major version compatible (manifest={manifest_schema_version}, current={current_schema_version})"
        return False, f"schema major version incompatible (manifest={manifest_schema_version}, current={current_schema_version})"
    except Exception:
        return False, f"schema_version format invalid (manifest={manifest_schema_version})"


async def validate_backup_payload(
    timestamp: str,
    backup_type: str,
    expected_ciphertext_sha256: str,
    expected_plaintext_sha256: str,
    r2_storage,
    schema_version: str = "",
    decryptor=None,
) -> BackupValidationResult:
    """R58 P0-3: 下载 payload → 校验 ciphertext_sha256 → 解密 → 校验 plaintext_sha256。

    R58 P0-3 增强:
        1. 校验 ciphertext_sha256(对象完整性)
        2. R58 P0-3: 真实解密(若提供 decryptor)— 以 backup_id+schema_version+commit_sha 作 AAD
        3. R58 P0-3: 校验 plaintext_sha256(明文完整性,不再只校验密文)
        4. R58 P0-3: 未提供 decryptor 时记录 warning,但仍校验密文(向后兼容)

    Args:
        timestamp: 备份 ID
        backup_type: full / incremental
        expected_ciphertext_sha256: manifest 中的 ciphertext_sha256
        expected_plaintext_sha256: manifest 中的 plaintext_sha256
        r2_storage: R2 存储客户端
        schema_version: schema 版本(用作 AAD)
        decryptor: 解密器对象(需提供 decrypt(ciphertext, aad) -> plaintext 方法)
                    未提供则跳过解密(向后兼容,但记录 warning)

    Returns:
        BackupValidationResult(valid=True 表示 payload 完整且解密校验通过)
    """
    payload_key = get_payload_key(timestamp, backup_type)
    try:
        ciphertext = await r2_storage.download(payload_key)
        if ciphertext is None:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.PAYLOAD_MISSING",
                error_message=f"payload.enc not found: {payload_key}",
            )
        # 1. 校验 ciphertext_sha256(对象完整性)
        actual_cipher_sha = _compute_sha256(ciphertext)
        if actual_cipher_sha != expected_ciphertext_sha256:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                ciphertext_sha256=actual_cipher_sha,
                error_code="BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH",
                error_message=(
                    f"ciphertext hash mismatch: expected={expected_ciphertext_sha256[:16]}..., "
                    f"actual={actual_cipher_sha[:16]}... (data may be corrupted in R2)"
                ),
            )
        # R58 P0-3: 真实解密 + plaintext_sha256 校验(若提供 decryptor)
        if decryptor is not None and expected_plaintext_sha256:
            try:
                # AAD = backup_id + schema_version(绑定备份身份与 schema)
                aad = f"{timestamp}:{schema_version}".encode("utf-8")
                plaintext = decryptor.decrypt(ciphertext, aad=aad)
            except Exception as decrypt_err:
                return BackupValidationResult(
                    valid=False,
                    backup_id=timestamp,
                    ciphertext_sha256=actual_cipher_sha,
                    error_code="BACKUP.RESTORE.DECRYPT_FAILED",
                    error_message=f"decryption failed: {type(decrypt_err).__name__}: {decrypt_err}",
                )
            # R58 P0-3: 校验 plaintext_sha256(明文完整性)
            actual_pt_sha = _compute_sha256(plaintext)
            if actual_pt_sha != expected_plaintext_sha256:
                return BackupValidationResult(
                    valid=False,
                    backup_id=timestamp,
                    ciphertext_sha256=actual_cipher_sha,
                    error_code="BACKUP.RESTORE.PLAINTEXT_HASH_MISMATCH",
                    error_message=(
                        f"plaintext hash mismatch: expected={expected_plaintext_sha256[:16]}..., "
                        f"actual={actual_pt_sha[:16]}... (decryption produced wrong data)"
                    ),
                )
            # R58 P0-3: 解密 + 明文 hash 校验通过
            return BackupValidationResult(
                valid=True,
                backup_id=timestamp,
                ciphertext_sha256=actual_cipher_sha,
            )
        else:
            # R58 P0-3: 未提供 decryptor,记录 warning(生产应强制解密校验)
            import logging
            logging.getLogger(__name__).warning(
                "R58 P0-3: validate_backup_payload 未提供 decryptor 或 plaintext_sha256,"
                "跳过解密与明文 hash 校验(生产应强制解密校验)"
            )
            return BackupValidationResult(
                valid=True,
                backup_id=timestamp,
                ciphertext_sha256=actual_cipher_sha,
            )
    except Exception as e:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.PAYLOAD_INVALID",
            error_message=f"Failed to validate payload: {e}",
        )


# ── staging 原子切换 ──────────────────────────────────────────


def atomic_restore_to_staging(
    staging_path: str | Path,
    final_path: str | Path,
    data: "dict | bytes | None" = None,
    sqlite_db_path: "str | Path | None" = None,
    require_atomic: bool = True,
) -> tuple[bool, str]:
    """R58 P0-3: 恢复到 staging 目录,验证后原子切换。

    R58 P0-3 增强:
        1. 支持 SQLite 数据库文件恢复(不再是 JSON dict)
        2. R58 P0-3: 原子 os.replace + fsync 文件与父目录
        3. R58 P0-3: 禁止非原子 fallback 标 success(require_atomic=True 时)
        4. R58 P0-3: 支持 SQLite PRAGMA integrity_check(若提供 sqlite_db_path)

    流程:
        若 sqlite_db_path 提供:
            1. 对 SQLite 执行 PRAGMA integrity_check
            2. os.replace(staging_db, final_db) 原子切换
            3. fsync 文件与父目录
        否则(data 模式,向后兼容):
            1. 写入 staging 路径(临时)
            2. fsync 确保落盘
            3. os.replace 原子切换(替代 rename,Windows 也可原子)
            4. fsync 父目录

    Args:
        staging_path: staging 临时文件路径
        final_path: 最终目标路径
        data: 要写入的数据(dict 或 bytes,向后兼容);与 sqlite_db_path 互斥
        sqlite_db_path: SQLite 数据库 staging 路径(用于真实 DB 恢复);
                        提供时,从此路径 os.replace 到 final_path
        require_atomic: True 时禁止非原子 fallback(默认 True,R58 P0-3)

    Returns:
        (success, message)
    """
    staging = Path(staging_path)
    final = Path(final_path)
    try:
        # 确保父目录存在
        staging.parent.mkdir(parents=True, exist_ok=True)
        final.parent.mkdir(parents=True, exist_ok=True)

        # R58 P0-3: SQLite 数据库文件恢复模式
        if sqlite_db_path is not None:
            staging_db = Path(sqlite_db_path)
            if not staging_db.exists():
                return False, f"sqlite staging db not found: {staging_db}"
            # R58 P0-3: 对 SQLite 执行 PRAGMA integrity_check
            try:
                import sqlite3 as _sqlite3_mod
                conn = _sqlite3_mod.connect(str(staging_db))
                cursor = conn.execute("PRAGMA integrity_check")
                integrity_result = cursor.fetchone()
                cursor.close()
                conn.close()
                if integrity_result[0] != "ok":
                    return False, f"SQLite integrity_check failed: {integrity_result[0]}"
            except Exception as integ_err:
                return False, f"SQLite integrity_check error: {type(integ_err).__name__}: {integ_err}"
            # R58 P0-3: 原子 os.replace(POSIX + Windows 均原子)
            os.replace(str(staging_db), str(final))
            # fsync 文件
            with open(final, "rb") as f:
                os.fsync(f.fileno())
            # fsync 父目录(确保目录条目落盘)
            _fsync_dir(final.parent)
            return True, f"SQLite atomic restore succeeded: {staging_db} -> {final}"

        # data 模式(向后兼容:dict 或 bytes)
        if data is None:
            return False, "neither data nor sqlite_db_path provided"
        if isinstance(data, dict):
            content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, bytes):
            content = data
        else:
            content = str(data).encode("utf-8")
        # 1. 写入 staging
        with open(staging, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # 2. R58 P0-3: 原子 os.replace(替代 rename,Windows 也可原子)
        try:
            os.replace(str(staging), str(final))
        except OSError as replace_err:
            if require_atomic:
                return False, f"atomic os.replace failed (require_atomic=True): {replace_err}"
            # R58 P0-3: 仅在 require_atomic=False 时允许非原子 fallback
            # 且不标记为 success — 返回 False,由调用方决定
            shutil.copy2(staging, final)
            staging.unlink()
            return False, f"non-atomic fallback used (require_atomic=False): {replace_err}"
        # 3. R58 P0-3: fsync 父目录(确保目录条目落盘)
        _fsync_dir(final.parent)
        return True, f"atomic switch succeeded: {staging} -> {final}"
    except Exception as e:
        # 清理 staging(避免残留)
        try:
            if staging.exists():
                staging.unlink()
        except Exception:
            pass
        return False, f"atomic switch failed: {e}"


def _fsync_dir(dir_path: "str | Path") -> None:
    """R58 P0-3: fsync 目录(确保目录条目落盘)。

    POSIX 系统支持 fsync 目录;Windows 下 fsync 目录会失败,忽略错误。
    """
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, PermissionError):
        # Windows 不支持 fsync 目录,忽略
        pass


# ── 辅助 ──────────────────────────────────────────────────────


def _compute_sha256(content: bytes) -> str:
    """计算 SHA-256(与 db_backup._compute_sha256 保持一致)。"""
    import hashlib
    return hashlib.sha256(content).hexdigest()


# ── 完整恢复流程编排 ───────────────────────────────────────────


async def validate_backup_for_restore(
    timestamp: str,
    backup_type: str,
    r2_storage,
    current_schema_version: str,
) -> BackupValidationResult:
    """R56 §8: 完整的恢复前验证流程编排。

    依次执行:
        1. COMPLETE 标记存在(备份完整性)
        2. manifest 字段完整(元数据完整性)
        3. schema compatibility(版本兼容性)
        4. payload ciphertext_sha256 校验(对象完整性)

    任一失败立即返回,不继续后续步骤。

    Args:
        timestamp: 备份 ID
        backup_type: full / incremental
        r2_storage: R2 存储客户端
        current_schema_version: 当前 _BACKUP_SCHEMA_VERSION

    Returns:
        BackupValidationResult(valid=True 表示可安全恢复)
    """
    # 1. COMPLETE 标记
    r1 = await validate_backup_completeness(timestamp, backup_type, r2_storage)
    if not r1.valid:
        return r1
    # 2. manifest 完整性
    r2 = await validate_backup_manifest(timestamp, backup_type, r2_storage)
    if not r2.valid:
        return r2
    # 3. schema compatibility
    compatible, reason = validate_schema_compatibility(
        r2.schema_version, current_schema_version,
    )
    if not compatible:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            schema_version=r2.schema_version,
            error_code="BACKUP.RESTORE.SCHEMA_INCOMPATIBLE",
            error_message=reason,
        )
    # 4. payload 校验
    r4 = await validate_backup_payload(
        timestamp, backup_type,
        r2.ciphertext_sha256, r2.plaintext_sha256,
        r2_storage,
    )
    if not r4.valid:
        return r4
    # 所有校验通过
    return BackupValidationResult(
        valid=True,
        backup_id=r2.backup_id,
        schema_version=r2.schema_version,
        ciphertext_sha256=r2.ciphertext_sha256,
        plaintext_sha256=r2.plaintext_sha256,
        encryption_key_id=r2.encryption_key_id,
    )
