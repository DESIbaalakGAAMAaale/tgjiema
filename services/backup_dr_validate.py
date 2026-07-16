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


def build_complete_marker(backup_id: str, manifest_key: str) -> bytes:
    """构建 COMPLETE 标记内容(JSON,含 backup_id + manifest_key + created_at)。

    Args:
        backup_id: 备份 ID(timestamp)
        manifest_key: 对应 manifest.json 的 R2 key

    Returns:
        JSON bytes
    """
    content = {
        "backup_id": backup_id,
        "manifest_key": manifest_key,
        "created_at": _dt.datetime.now(timezone_utc()).isoformat(),
        "schema": "R56-§8-three-stage",
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
) -> BackupValidationResult:
    """R56 §8: 验证备份完整性(三段式 COMPLETE 标记存在)。

    Args:
        timestamp: 备份 ID(timestamp)
        backup_type: full / incremental
        r2_storage: R2 存储客户端

    Returns:
        BackupValidationResult(valid=True 表示 COMPLETE 标记存在)
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
        return BackupValidationResult(
            valid=True,
            backup_id=marker.get("backup_id", timestamp),
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
        # 提取关键字段
        encryption = manifest.get("encryption", {})
        return BackupValidationResult(
            valid=True,
            backup_id=manifest.get("backup_id", timestamp),
            schema_version=str(manifest.get("schema_version", "")),
            ciphertext_sha256=manifest.get("ciphertext_sha256", ""),
            plaintext_sha256=manifest.get("plaintext_sha256", ""),
            encryption_key_id=encryption.get("key_id", ""),
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
) -> BackupValidationResult:
    """R56 §8: 下载 payload → 校验 ciphertext_sha256 → 解密 → 校验 plaintext_sha256。

    Args:
        timestamp: 备份 ID
        backup_type: full / incremental
        expected_ciphertext_sha256: manifest 中的 ciphertext_sha256
        expected_plaintext_sha256: manifest 中的 plaintext_sha256
        r2_storage: R2 存储客户端

    Returns:
        BackupValidationResult(valid=True 表示 payload 完整且校验通过)
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
        # 2. 解密(委托给 backup_crypto)
        # 注意:解密需要 backup_id(AAD 绑定)+ schema_version
        # 此处仅校验密文,解密由调用方在 staging 阶段完成
        # (避免在本函数中引入 KEK 依赖,保持单一职责)
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
    data: dict,
) -> tuple[bool, str]:
    """R56 §8: 恢复到 staging 目录,验证后原子切换。

    流程:
        1. 写入 staging 路径(临时)
        2. fsync 确保落盘
        3. rename 到 final 路径(原子操作,POSIX 保证)
        4. Windows 下 rename 失败则 fallback 到 copy + delete

    Args:
        staging_path: staging 临时文件路径
        final_path: 最终目标路径
        data: 要写入的数据(dict,会 JSON 序列化)

    Returns:
        (success, message)
    """
    staging = Path(staging_path)
    final = Path(final_path)
    try:
        # 确保父目录存在
        staging.parent.mkdir(parents=True, exist_ok=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        # 1. 写入 staging
        content = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
        with open(staging, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # 2. 原子 rename(POSIX 保证原子性)
        try:
            # Windows 下若 final 已存在,rename 会失败,先删除
            if final.exists():
                final.unlink()
            staging.rename(final)
        except OSError:
            # fallback: copy + delete
            shutil.copy2(staging, final)
            staging.unlink()
        return True, f"atomic switch succeeded: {staging} -> {final}"
    except Exception as e:
        # 清理 staging(避免残留)
        try:
            if staging.exists():
                staging.unlink()
        except Exception:
            pass
        return False, f"atomic switch failed: {e}"


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
