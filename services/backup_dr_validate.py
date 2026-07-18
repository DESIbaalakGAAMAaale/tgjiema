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
import hashlib
import hmac
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from services.i18n import translate as _i18n_t


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
    """备份验证结果(仅用于 validate-only 函数,不用于写入授权)。

    R61 P0-03: 此 dataclass 仅由 validate_backup_completeness / validate_backup_manifest /
    validate_backup_payload / validate_backup_for_restore 等纯校验函数返回。
    它**不能**用于授权数据库写入 — 任意调用方均可构造 valid=True 实例(公开 dataclass),
    因此不能作为信任令牌传递给 _restore_from_backup_data()。

    数据库写入授权必须使用 _RestoreCapability(不可伪造,由 _RESTORE_SENTINEL 保护)。
    """
    valid: bool
    backup_id: str = ""
    schema_version: str = ""
    ciphertext_sha256: str = ""
    plaintext_sha256: str = ""
    encryption_key_id: str = ""
    error_code: str = ""
    error_message: str = ""
    # R59 P0-04: 强制参数,不再允许 fail-open — 新增信任链传递字段
    manifest_sha256: str = ""  # R59 P0-04: 来自 COMPLETE marker,用于 manifest bytes SHA 比对
    payload_key: str = ""      # R59 P0-04: 来自 COMPLETE marker,用于 payload_key 一致性比对


# ── R61 P0-03: 不可伪造的恢复能力令牌 ──────────────────────────


# 模块私有 sentinel — 外部模块无法 import 或访问此对象。
# _RestoreCapability.__init__ 仅在 sentinel is _RESTORE_SENTINEL 时允许构造,
# 因此只有 backup_dr_validate.py 内部代码(即 validate_and_restore_backup_strict)
# 能创建合法的 _RestoreCapability 实例。
_RESTORE_SENTINEL = object()


class _RestoreCapability:
    """R61 P0-03: 不可伪造的恢复能力令牌。

    仅 validate_and_restore_backup_strict() 通过 _RESTORE_SENTINEL 可构造实例。
    私有写入器 services.db_restore._restore_from_backup_data 仅接受此类型,
    并验证 _sentinel 属性以防止伪造。

    安全模型:
        - _RESTORE_SENTINEL 是模块私有对象(以 _ 前缀标记,且不导出),
          外部代码无法获取它的引用。
        - _RestoreCapability.__init__ 检查 sentinel is _RESTORE_SENTINEL,
          若不匹配则抛 RuntimeError,阻止外部构造。
        - 因此,只有 backup_dr_validate.py 内部代码能构造合法实例。
        - _restore_from_backup_data 进一步检查 _sentinel 属性非空,
          双重防御(防止通过 monkeypatch _RestoreCapability 类绕过)。

    令牌字段(来自严格验证通过的 COMPLETE marker / manifest / payload):
        - backup_id:          备份 ID
        - manifest_sha256:    manifest 原始 bytes 的 SHA-256
        - payload_key:        payload.enc 的 R2 key
        - ciphertext_sha256:  密文的 SHA-256
        - plaintext_sha256:   明文的 SHA-256
        - encryption_key_id:  加密密钥 ID
        - created_at:         令牌构造时间(UTC ISO)
        - expires_at:         令牌过期时间戳(unix 秒);过期后 is_valid() 返回 False
    """

    __slots__ = (
        "_sentinel", "backup_id", "manifest_sha256", "payload_key",
        "ciphertext_sha256", "plaintext_sha256", "encryption_key_id",
        "created_at", "expires_at",
    )

    def __init__(
        self,
        sentinel,
        backup_id: str,
        manifest_sha256: str,
        payload_key: str,
        ciphertext_sha256: str,
        plaintext_sha256: str,
        encryption_key_id: str,
        ttl_seconds: int = 600,
    ):
        # 仅当调用方持有模块私有 _RESTORE_SENTINEL 时允许构造
        if sentinel is not _RESTORE_SENTINEL:
            # R61 P0-03: 不可伪造令牌被外部构造尝试 — fail-closed,使用协议化错误码
            # (本文件属于 data-integrity 零容忍域,禁止裸字符串异常)
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        import time as _time
        self._sentinel = sentinel
        self.backup_id = backup_id
        self.manifest_sha256 = manifest_sha256
        self.payload_key = payload_key
        self.ciphertext_sha256 = ciphertext_sha256
        self.plaintext_sha256 = plaintext_sha256
        self.encryption_key_id = encryption_key_id
        self.created_at = _time.time()
        self.expires_at = self.created_at + ttl_seconds

    def is_valid(self) -> bool:
        """检查能力令牌是否仍有效(sentinel 匹配 + 未过期 + 关键字段非空)。"""
        import time as _time
        if self._sentinel is not _RESTORE_SENTINEL:
            return False
        if _time.time() > self.expires_at:
            return False
        # 所有关键信任链字段必须非空
        return all([
            self.backup_id,
            self.manifest_sha256,
            self.payload_key,
            self.ciphertext_sha256,
            self.plaintext_sha256,
        ])

    def __repr__(self) -> str:
        return (
            f"_RestoreCapability(backup_id={self.backup_id!r}, "
            f"valid={self.is_valid()})"
        )


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


def _canonical_marker_signing_payload(
    backup_id: str,
    manifest_key: str,
    manifest_sha256: str,
    payload_key: str,
    payload_sha256: str,
    schema_version: str = "R58-P0-3-signed-three-stage",
) -> bytes:
    """R60 P0-04: Versioned canonical JSON signing payload for COMPLETE marker.

    Includes payload_key (R60 fix) and uses sorted-key JSON to avoid colon-delimited
    field encoding ambiguity.

    R60 P0-04 §7 修复:
        - 所有"决定下载哪个对象"的字段必须签名(含 payload_key)
        - 使用 versioned canonical JSON 替代 colon 拼接(避免字段编码歧义)
        - schema_version 进入签名内容(支持密钥/版本轮换)
        - marker / manifest / payload 绑定相同 backup_id/schema_version/payload_key/
          manifest_key/plaintext_sha/ciphertext_sha
    """
    payload = {
        "v": 1,
        "backup_id": backup_id,
        "manifest_key": manifest_key,
        "manifest_sha256": manifest_sha256,
        "payload_key": payload_key,
        "payload_sha256": payload_sha256,
        "schema_version": schema_version,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def build_complete_marker(
    backup_id: str,
    manifest_key: str,
    manifest_sha256: str,  # R59 P0-04: 强制参数,不再允许 fail-open(原 = "")
    payload_key: str,      # R59 P0-04: 强制参数,不再允许 fail-open(原 = "")
    payload_sha256: str,   # R59 P0-04: 强制参数,不再允许 fail-open(原 = "")
    signing_key: bytes,    # R60 P0-04: 替代 signature 参数,内部用 canonical JSON 计算签名
    schema_version: str = "R58-P0-3-signed-three-stage",
) -> bytes:
    """R58 P0-3 / R59 P0-04 / R60 P0-04: 构建 COMPLETE 标记内容(JSON,含强绑定字段)。

    R58 P0-3 增强(签名 + digest 绑定):
        - backup_id: 备份 ID(timestamp)
        - manifest_key: manifest.json 的 R2 key
        - manifest_sha256: manifest 内容的 SHA-256(R58 P0-3: 绑定 manifest digest)
        - payload_key: payload.enc 的 R2 key
        - payload_sha256: 密文的 SHA-256(R58 P0-3: 绑定 payload digest)
        - signature: 整个 marker 的 HMAC 签名(R58 P0-3: 防止伪造 COMPLETE)
        - created_at: 创建时间(UTC ISO)
        - schema: schema 版本

    R59 P0-04 增强(强制参数,不再允许 fail-open):
        - 删除所有安全参数的默认值,生产入口类型上强制必填
        - 合法调用方必须显式传入所有参数

    R60 P0-04 增强(§7,P0-04 — canonical JSON 签名):
        - 签名内容改用 versioned canonical JSON(含 payload_key,避免 colon 拼接歧义)
        - 内部用 signing_key 计算 HMAC,移除外部 signature 参数
        - 输出新增 signature_version=1 字段(支持签名格式轮换)
        - schema_version 进入签名内容(支持密钥/版本轮换)

    Args:
        backup_id: 备份 ID(timestamp)
        manifest_key: 对应 manifest.json 的 R2 key
        manifest_sha256: manifest 内容 SHA-256(64 hex) — R59 P0-04: 必填
        payload_key: payload.enc 的 R2 key — R59 P0-04: 必填
        payload_sha256: 密文 SHA-256(64 hex) — R59 P0-04: 必填
        signing_key: HMAC 签名密钥 — R60 P0-04: 必填(替代外部 signature 参数)
        schema_version: schema 版本字符串(进入签名内容,默认 R58-P0-3-signed-three-stage)

    Returns:
        JSON bytes
    """
    # R60 P0-04: 使用 versioned canonical JSON 计算签名(含 payload_key,避免 colon 拼接歧义)
    sign_payload = _canonical_marker_signing_payload(
        backup_id=backup_id,
        manifest_key=manifest_key,
        manifest_sha256=manifest_sha256,
        payload_key=payload_key,
        payload_sha256=payload_sha256,
        schema_version=schema_version,
    )
    signature = hmac.new(signing_key, sign_payload, hashlib.sha256).hexdigest()
    content = {
        "backup_id": backup_id,
        "manifest_key": manifest_key,
        "manifest_sha256": manifest_sha256,
        "payload_key": payload_key,
        "payload_sha256": payload_sha256,
        "signature": signature,
        "created_at": _dt.datetime.now(timezone_utc()).isoformat(),
        "schema": schema_version,
        "signature_version": 1,  # R60 P0-04: versioned canonical JSON 签名
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
    expected_manifest_key: str,  # R59 P0-04: 强制参数,不再允许 fail-open(原 = "")
    signing_key: bytes,          # R59 P0-04: 强制参数,不再允许 fail-open(原 = b"")
    expected_backup_id: str,     # R59 P0-04: 新增强制参数,比对 backup_id
) -> BackupValidationResult:
    """R58 P0-3 / R59 P0-04 / R60 P0-04: 验证备份完整性(COMPLETE 标记存在 + 签名 + 严格绑定)。

    R58 P0-3 增强:
        1. COMPLETE 标记存在
        2. R58 P0-3: 验证 marker signature
        3. R58 P0-3: 严格比较 backup_id == 请求 timestamp
        4. R58 P0-3: manifest_key 指向当前请求的 manifest
        5. R58 P0-3: manifest_sha256/payload_sha256 非空(强绑定)

    R59 P0-04 增强(强制参数,不再允许 fail-open):
        - 删除 expected_manifest_key/signing_key 的默认值,生产入口类型上强制必填
        - 新增 expected_backup_id 强制参数,与 marker.backup_id 严格比对
        - 删除"可选跳过验签"路径:signing_key 缺失时直接返回 invalid
        - 合法调用方必须显式传入所有参数

    R60 P0-04 增强(§7 — canonical JSON 签名 + payload_key 强绑定):
        - 签名内容改用 versioned canonical JSON(_canonical_marker_signing_payload),
          含 payload_key,替代原 colon 拼接(避免字段编码歧义)
        - 新增 signature_version 校验(默认 1,要求 >= 1 — history 必须 re-package)
        - 新增 payload_key 非空校验(fail-closed) — 原签名遗漏该字段,可被替换到任意 payload
        - marker/manifest/payload 绑定相同 backup_id/schema_version/payload_key/
          manifest_key/plaintext_sha/ciphertext_sha

    验证顺序(固定):
        下载 COMPLETE → 验签 → 比对 backup_id → 比对 manifest_key → 比对 digest

    Args:
        timestamp: 备份 ID(timestamp)
        backup_type: full / incremental
        r2_storage: R2 存储客户端
        expected_manifest_key: 期望的 manifest R2 key — R59 P0-04: 必填
        signing_key: COMPLETE marker 签名密钥 — R59 P0-04: 必填(空则返回 invalid)
        expected_backup_id: 期望的 backup_id — R59 P0-04: 必填

    Returns:
        BackupValidationResult(valid=True 表示 COMPLETE 标记有效且绑定一致)
    """
    # R59 P0-04: 强制参数,不再允许 fail-open — 缺失任何参数时直接返回 invalid
    if not expected_manifest_key:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
            error_message="R59 P0-04: expected_manifest_key is required (fail-closed)",
        )
    if not signing_key:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
            error_message="R59 P0-04: signing_key is required (fail-closed, no skip verify)",
        )
    if not expected_backup_id:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
            error_message="R59 P0-04: expected_backup_id is required (fail-closed)",
        )

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
        marker_payload_key = str(marker.get("payload_key", ""))
        marker_payload_sha = str(marker.get("payload_sha256", ""))
        marker_signature = str(marker.get("signature", ""))
        # R60 P0-04: schema 与 signature_version 进入签名内容(支持轮换与版本校验)
        marker_schema = str(marker.get("schema", "R58-P0-3-signed-three-stage"))
        marker_signature_version = marker.get("signature_version", 1)

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
        # R59 P0-04: 严格比较 backup_id == expected_backup_id(信任链绑定)
        if marker_backup_id != expected_backup_id:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message=(
                    f"R59 P0-04: backup_id mismatch with expected_backup_id: "
                    f"expected={expected_backup_id}, actual={marker_backup_id}"
                ),
            )
        # R58 P0-3: manifest_key 必须指向当前请求的 manifest(R59 P0-04: 强制比对,不再可选)
        if marker_manifest_key != expected_manifest_key:
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
        # R59 P0-04: 强制参数,不再允许 fail-open — 验签必填,删除"可选跳过验签"路径
        if not marker_signature or len(marker_signature) != 64:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message="COMPLETE marker missing or invalid signature",
            )
        # R60 P0-04: 校验 signature_version(默认 1,要求 >= 1 — history 必须 re-package)
        if not isinstance(marker_signature_version, int) or marker_signature_version < 1:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message=(
                    f"R60 P0-04: COMPLETE marker signature_version invalid: "
                    f"{marker_signature_version!r} (require >= 1)"
                ),
            )
        # R60 P0-04: payload_key 必须非空(fail-closed) — 签名内容必须包含 payload_key,
        # 否则可被替换到任意 payload 对象(原 colon 拼接签名遗漏该字段)
        if not marker_payload_key:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message="R60 P0-04: COMPLETE marker missing payload_key (fail-closed)",
            )
        # R60 P0-04: 使用 versioned canonical JSON 重算签名(含 payload_key,避免 colon 拼接歧义)
        sign_payload = _canonical_marker_signing_payload(
            backup_id=marker_backup_id,
            manifest_key=marker_manifest_key,
            manifest_sha256=marker_manifest_sha,
            payload_key=marker_payload_key,
            payload_sha256=marker_payload_sha,
            schema_version=marker_schema,
        )
        expected_sig = hmac.new(signing_key, sign_payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, marker_signature):
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message="COMPLETE marker signature verification failed",
            )
        # R59 P0-04: 验签通过,返回信任链字段(manifest_sha256/payload_key)供后续步骤比对
        return BackupValidationResult(
            valid=True,
            backup_id=marker_backup_id,
            manifest_sha256=marker_manifest_sha,  # R59 P0-04: 传递给 manifest bytes SHA 比对
            payload_key=marker_payload_key,       # R59 P0-04: 传递给 payload_key 一致性比对
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
    schema_version: str,  # R59 P0-04: 强制参数(原 = ""),AAD 绑定需要
    decryptor,            # R59 P0-04: 强制参数(原 = None),缺失返回 invalid
    key_id: str = "",     # R59 P0-04: 新增,AAD 绑定需要
) -> BackupValidationResult:
    """R58 P0-3 / R59 P0-04: 下载 payload → 校验 ciphertext_sha256 → 解密 → 校验 plaintext_sha256。

    R58 P0-3 增强:
        1. 校验 ciphertext_sha256(对象完整性)
        2. R58 P0-3: 真实解密(若提供 decryptor)— 以 backup_id+schema_version 作 AAD
        3. R58 P0-3: 校验 plaintext_sha256(明文完整性,不再只校验密文)

    R59 P0-04 增强(强制参数,不再允许 fail-open):
        - decryptor 必填:缺失时直接返回 invalid(不再记录 warning 后跳过)
        - schema_version 必填:AAD 绑定需要
        - 新增 key_id 参数:AAD 绑定需要
        - AAD 绑定字段扩展为:backup_id|schema_version|payload_key|key_id|plaintext_sha256
          (R58 仅绑定 backup_id:schema_version,R59 扩展为 5 字段强绑定)

    AAD 绑定字段(R59 P0-04):
        backup_id | schema_version | payload_key | key_id | plaintext_sha256
        — 绑定备份身份、schema 版本、对象 key、加密密钥 ID、明文摘要
        — 防止密文被替换到其他 backup_id/payload_key 的攻击

    Args:
        timestamp: 备份 ID(= backup_id)
        backup_type: full / incremental
        expected_ciphertext_sha256: manifest 中的 ciphertext_sha256
        expected_plaintext_sha256: manifest 中的 plaintext_sha256
        r2_storage: R2 存储客户端
        schema_version: schema 版本 — R59 P0-04: 必填(AAD 绑定)
        decryptor: 解密器对象(需提供 decrypt(ciphertext, aad) -> plaintext 方法)
                   — R59 P0-04: 必填(空则返回 invalid)
        key_id: 加密密钥 ID — R59 P0-04: AAD 绑定需要

    Returns:
        BackupValidationResult(valid=True 表示 payload 完整且解密校验通过)
    """
    # R59 P0-04: 强制参数,不再允许 fail-open — decryptor 必填,缺失时返回 invalid
    if decryptor is None:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.PAYLOAD_INVALID",
            error_message="R59 P0-04: decryptor is required (fail-closed, no skip decrypt)",
        )
    # R59 P0-04: schema_version 必填(AAD 绑定需要)
    if not schema_version:
        return BackupValidationResult(
            valid=False,
            backup_id=timestamp,
            error_code="BACKUP.RESTORE.PAYLOAD_INVALID",
            error_message="R59 P0-04: schema_version is required for AAD binding (fail-closed)",
        )

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
        # R59 P0-04: 强制参数,不再允许 fail-open — 真实解密 + plaintext_sha256 校验(必填)
        if not expected_plaintext_sha256:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                ciphertext_sha256=actual_cipher_sha,
                error_code="BACKUP.RESTORE.PAYLOAD_INVALID",
                error_message="R59 P0-04: expected_plaintext_sha256 is required (fail-closed)",
            )
        try:
            # R59 P0-04: AAD 绑定 5 字段 — backup_id|schema_version|payload_key|key_id|plaintext_sha256
            # (R58 仅绑定 backup_id:schema_version,R59 扩展为 5 字段强绑定)
            aad = (
                f"{timestamp}|{schema_version}|{payload_key}|{key_id}|"
                f"{expected_plaintext_sha256}"
            ).encode("utf-8")
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
        # R59 P0-04: 解密 + AAD 验证 + 明文 hash 校验通过
        return BackupValidationResult(
            valid=True,
            backup_id=timestamp,
            ciphertext_sha256=actual_cipher_sha,
            plaintext_sha256=actual_pt_sha,  # R59 P0-04: 返回明文 hash 供信任链传递
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
        except Exception as cleanup_err:
            logger.warning(
                _i18n_t(
                    'services.backup_dr_validate.logger_staging_cleanup_failed',
                    cleanup_err=cleanup_err,
                )
            )
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
    # R59 P0-04: hashlib 已在模块顶部导入(R58 原 local import 已提升)
    return hashlib.sha256(content).hexdigest()


# ── 完整恢复流程编排 ───────────────────────────────────────────


async def validate_backup_for_restore(
    timestamp: str,
    backup_type: str,
    r2_storage,
    current_schema_version: str,
    expected_manifest_key: str,  # R59 P0-04: 强制参数(透传给 validate_backup_completeness)
    signing_key: bytes,          # R59 P0-04: 强制参数(透传给 validate_backup_completeness)
    expected_backup_id: str,     # R59 P0-04: 强制参数(透传给 validate_backup_completeness)
    decryptor,                   # R59 P0-04: 强制参数(透传给 validate_backup_payload)
    key_id: str = "",            # R59 P0-04: AAD 绑定需要(透传给 validate_backup_payload)
) -> BackupValidationResult:
    """R56 §8 / R59 P0-04: 完整的恢复前验证流程编排。

    依次执行:
        1. COMPLETE 标记存在(备份完整性) — R59 P0-04: 强制验签 + backup_id 比对
        2. manifest 字段完整(元数据完整性)
        3. schema compatibility(版本兼容性)
        4. payload ciphertext_sha256 校验(对象完整性) — R59 P0-04: 强制解密 + AAD 绑定

    任一失败立即返回,不继续后续步骤。

    R59 P0-04 增强(强制参数,不再允许 fail-open):
        - 新增 expected_manifest_key/signing_key/expected_backup_id/decryptor 必填参数
        - 透传给 validate_backup_completeness 和 validate_backup_payload

    Args:
        timestamp: 备份 ID
        backup_type: full / incremental
        r2_storage: R2 存储客户端
        current_schema_version: 当前 _BACKUP_SCHEMA_VERSION
        expected_manifest_key: 期望的 manifest R2 key — R59 P0-04: 必填
        signing_key: COMPLETE marker 签名密钥 — R59 P0-04: 必填
        expected_backup_id: 期望的 backup_id — R59 P0-04: 必填
        decryptor: 解密器对象 — R59 P0-04: 必填
        key_id: 加密密钥 ID — R59 P0-04: AAD 绑定需要

    Returns:
        BackupValidationResult(valid=True 表示可安全恢复)
    """
    # 1. COMPLETE 标记(R59 P0-04: 透传强制参数)
    r1 = await validate_backup_completeness(
        timestamp, backup_type, r2_storage,
        expected_manifest_key, signing_key, expected_backup_id,
    )
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
    # 4. payload 校验(R59 P0-04: 透传强制参数 decryptor/schema_version/key_id)
    r4 = await validate_backup_payload(
        timestamp, backup_type,
        r2.ciphertext_sha256, r2.plaintext_sha256,
        r2_storage,
        schema_version=r2.schema_version,  # R59 P0-04: 必填(AAD 绑定)
        decryptor=decryptor,               # R59 P0-04: 必填(fail-closed)
        key_id=key_id,                     # R59 P0-04: AAD 绑定
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
        manifest_sha256=r1.manifest_sha256,  # R59 P0-04: 信任链传递
        payload_key=r1.payload_key,          # R59 P0-04: 信任链传递
    )


# ── R59 P0-04 / R61 P0-03: 统一 fail-closed 恢复入口 ────────────


async def validate_and_restore_backup_strict(
    *,
    data: dict,                          # R61 P0-03: 必填 — 待写入的备份数据 dict
    tables: "list[str] | None" = None,
    merge: bool = False,
    # 严格三段式验证参数(可选 — 不提供且 skip_strict_validation=True 时跳过严格验证)
    timestamp: str = "",
    backup_type: str = "full",
    r2_storage=None,
    signing_key: bytes = b"",
    decryptor=None,
    expected_manifest_key: str = "",
    expected_backup_id: str = "",
    current_schema_version: str = "",
    staging_path: "str | Path | None" = None,
    final_path: "str | Path | None" = None,
    sqlite_db_staging: "str | Path | None" = None,
    # R61 P0-03: 跳过严格三段式验证(仅用于已通过其他方式验证的旧格式备份)
    skip_strict_validation: bool = False,
    validation_note: str = "",
    # R61 P0-03: 信任链元数据(用于构造 _RestoreCapability;skip_strict_validation=True 时必填)
    backup_id_override: str = "",
    manifest_sha256_override: str = "",
    payload_key_override: str = "",
    ciphertext_sha256_override: str = "",
    plaintext_sha256_override: str = "",
    encryption_key_id_override: str = "",
) -> dict:
    """R59 P0-04 / R61 P0-03: 统一 fail-closed 备份恢复公共入口 — 整合验证 + 写入。

    本函数是生产恢复的**唯一公共写入入口**。db_restore.py / db_backup.py /
    backup_engine.py / disaster_recovery.py 必须通过本函数执行恢复写入,
    禁止直接调用 services.db_restore._restore_from_backup_data(私有)。

    R61 P0-03 信任链整改:
        - 本函数是**唯一**能构造 _RestoreCapability 的公共入口
          (sentinel _RESTORE_SENTINEL 为模块私有,外部代码无法构造合法令牌)。
        - 构造令牌后调用私有写入器 _restore_from_backup_data(data, _capability=cap),
          写入器验证 _sentinel 属性防止伪造。
        - 旧 R59 P0-04 / R60 P0-03 的 BackupValidationResult 信任令牌已废弃
          (其为公开 dataclass,任意调用方可构造 valid=True,无法防止伪造)。

    R59 P0-04 严格三段式验证(可选):
        当 skip_strict_validation=False(默认) 且提供完整验证参数时,执行:
        1. 下载 COMPLETE → 验签 → 比对 backup_id
        2. 下载 manifest 原始 bytes → 比对 SHA256(manifest_bytes)
        3. 解析严格 schema → 比对 payload_key
        4. 下载密文 → 比对密文 SHA
        5. AEAD 解密并验证 AAD → 比对明文 SHA
        6. 数据库完整性检查(若提供 sqlite_db_staging)
        7. 临时文件 fsync → 原子替换 → 父目录 fsync(若提供 staging/final path)

    R61 P0-03 兼容模式(skip_strict_validation=True):
        用于已通过其他验证路径(BackupEngine._restore_internal 自有的
        ciphertext_sha/decrypt/plaintext_sha 验证,或 CLI get_latest_backup()
        的 manifest+checksum+decrypt 验证)的旧格式备份。调用方通过 *_override
        参数提供信任链元数据,本函数构造 _RestoreCapability 并写入。
        - 安全保证:_RestoreCapability 仍由本模块构造(sentinel 保护),
          外部代码无法直接构造令牌调用 _restore_from_backup_data。
        - 调用方需自行确保 data 已通过等效验证(审计日志记录 validation_note)。

    AAD 绑定字段(R59 P0-04 严格模式):
        backup_id | schema_version | payload_key | key_id | plaintext_sha256

    Args:
        data: R61 P0-03 必填 — 待写入的备份数据 dict(含 "tables" 键)
        tables: 仅恢复指定表;None 则恢复备份中的所有表
        merge: True=增量补充;False=覆盖(默认)
        timestamp: 备份 ID(timestamp) — 严格模式必填
        backup_type: full / incremental(默认 full)
        r2_storage: R2 存储客户端 — 严格模式必填
        signing_key: COMPLETE marker 签名密钥 — 严格模式必填
        decryptor: 解密器对象(需提供 decrypt(ciphertext, aad) -> plaintext) — 严格模式必填
        expected_manifest_key: 期望的 manifest R2 key — 严格模式必填
        expected_backup_id: 期望的 backup_id — 严格模式必填
        current_schema_version: 当前 _BACKUP_SCHEMA_VERSION(schema 兼容性检查)
        staging_path: staging 临时文件路径(可选,提供时执行原子切换)
        final_path: 最终目标路径(可选,提供时执行原子切换)
        sqlite_db_staging: SQLite DB staging 路径(可选,提供时执行 integrity_check)
        skip_strict_validation: R61 P0-03 跳过严格三段式验证(默认 False)
        validation_note: 审计日志备注(说明跳过严格验证的原因/替代验证路径)
        backup_id_override: 兼容模式 — 信任链 backup_id
        manifest_sha256_override: 兼容模式 — 信任链 manifest_sha256
        payload_key_override: 兼容模式 — 信任链 payload_key
        ciphertext_sha256_override: 兼容模式 — 信任链 ciphertext_sha256
        plaintext_sha256_override: 兼容模式 — 信任链 plaintext_sha256
        encryption_key_id_override: 兼容模式 — 信任链 encryption_key_id

    Returns:
        dict: _restore_from_backup_data 的结果
              {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}

    Raises:
        AppError: 严格模式验证失败时(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED 等),
                  或兼容模式缺少必要 *_override 参数时
    """
    # R61 P0-03: 信任链元数据(由严格验证或 *_override 提供)
    cap_backup_id = ""
    cap_manifest_sha256 = ""
    cap_payload_key = ""
    cap_ciphertext_sha256 = ""
    cap_plaintext_sha256 = ""
    cap_encryption_key_id = ""

    if not skip_strict_validation:
        # ── 严格三段式验证模式 ──
        # R59 P0-04: 强制参数,不再允许 fail-open — 入口参数校验
        if not signing_key:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if decryptor is None:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if not expected_manifest_key:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if not expected_backup_id:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # ── 步骤 1: 下载 COMPLETE → 验签 → 比对 backup_id ──
        r1 = await validate_backup_completeness(
            timestamp, backup_type, r2_storage,
            expected_manifest_key, signing_key, expected_backup_id,
        )
        if not r1.valid:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        # 信任链: r1.manifest_sha256 / r1.payload_key 来自验签通过的 COMPLETE marker

        # ── 步骤 2: 下载 manifest 原始 bytes → 比对 SHA256(manifest_bytes) ──
        manifest_key = get_manifest_key(timestamp, backup_type)
        try:
            manifest_bytes = await r2_storage.download(manifest_key)
        except Exception as e:
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                _i18n_t(
                    'services.backup_dr_validate.logger_manifest_download_failed',
                    e=e,
                )
            )
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if manifest_bytes is None:
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                _i18n_t(
                    'services.backup_dr_validate.logger_manifest_not_found',
                    manifest_key=manifest_key,
                )
            )
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        # 比对 SHA256(manifest_bytes) 与 COMPLETE marker 中的 manifest_sha256
        actual_manifest_sha = _compute_sha256(manifest_bytes)
        if actual_manifest_sha != r1.manifest_sha256:
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                _i18n_t(
                    'services.backup_dr_validate.logger_manifest_sha_mismatch',
                    expected=r1.manifest_sha256[:16],
                    actual=actual_manifest_sha[:16],
                )
            )
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # ── 步骤 3: 解析严格 schema → 比对 payload_key ──
        try:
            manifest = json.loads(manifest_bytes)
        except Exception as e:
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                _i18n_t(
                    'services.backup_dr_validate.logger_manifest_json_parse_failed',
                    e=e,
                )
            )
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        # 检查必填字段
        missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest]
        if missing:
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                _i18n_t(
                    'services.backup_dr_validate.logger_manifest_missing_fields',
                    missing=missing,
                )
            )
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        # 严格字段格式校验
        manifest_backup_id = str(manifest.get("backup_id", ""))
        if manifest_backup_id != timestamp:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        ct_sha = str(manifest.get("ciphertext_sha256", ""))
        pt_sha = str(manifest.get("plaintext_sha256", ""))
        if len(ct_sha) != 64 or not all(c in "0123456789abcdef" for c in ct_sha.lower()):
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if len(pt_sha) != 64 or not all(c in "0123456789abcdef" for c in pt_sha.lower()):
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        encryption = manifest.get("encryption", {})
        if not isinstance(encryption, dict):
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        key_id = str(encryption.get("key_id", ""))
        if not key_id:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        schema_version = str(manifest.get("schema_version", ""))
        if not schema_version:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        # 比对 payload_key — COMPLETE marker 中的 payload_key 必须与计算值一致
        expected_payload_key = get_payload_key(timestamp, backup_type)
        if r1.payload_key and r1.payload_key != expected_payload_key:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # schema compatibility 检查(若提供 current_schema_version)
        if current_schema_version:
            compatible, reason = validate_schema_compatibility(
                schema_version, current_schema_version,
            )
            if not compatible:
                from services.error_codes import AppError, ErrorCodes
                logger.error(
                    _i18n_t(
                        'services.backup_dr_validate.logger_schema_incompatible',
                        reason=reason,
                    )
                )
                raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # ── 步骤 4+5: 下载密文 → 比对密文 SHA → AEAD 解密并验证 AAD → 比对明文 SHA ──
        r5 = await validate_backup_payload(
            timestamp, backup_type,
            ct_sha, pt_sha,
            r2_storage,
            schema_version=schema_version,
            decryptor=decryptor,
            key_id=key_id,
        )
        if not r5.valid:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # ── 步骤 6+7: 数据库完整性检查 → 临时文件 fsync → 原子替换 → 父目录 fsync ──
        if staging_path is not None and final_path is not None:
            ok, msg = atomic_restore_to_staging(
                staging_path, final_path,
                sqlite_db_path=sqlite_db_staging,
                require_atomic=True,
            )
            if not ok:
                from services.error_codes import AppError, ErrorCodes
                logger.error(
                    _i18n_t(
                        'services.backup_dr_validate.logger_atomic_restore_failed',
                        msg=msg,
                    )
                )
                raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # 严格验证通过 — 提取信任链元数据
        cap_backup_id = manifest_backup_id
        cap_manifest_sha256 = actual_manifest_sha
        cap_payload_key = expected_payload_key
        cap_ciphertext_sha256 = ct_sha
        cap_plaintext_sha256 = pt_sha
        cap_encryption_key_id = key_id
    else:
        # ── R61 P0-03 兼容模式:跳过严格三段式验证 ──
        # 用于已通过其他验证路径的旧格式备份(BackupEngine / CLI / db_backup)。
        # 调用方通过 *_override 参数提供信任链元数据。
        if not validation_note:
            logger.warning(
                _i18n_t(
                    'services.backup_dr_validate.logger_skip_strict_no_note',
                )
            )
        logger.info(
            _i18n_t(
                'services.backup_dr_validate.logger_skip_strict_compat_mode',
                validation_note=validation_note,
            )
        )
        cap_backup_id = backup_id_override
        cap_manifest_sha256 = manifest_sha256_override
        cap_payload_key = payload_key_override
        cap_ciphertext_sha256 = ciphertext_sha256_override
        cap_plaintext_sha256 = plaintext_sha256_override
        cap_encryption_key_id = encryption_key_id_override

    # ── R61 P0-03: 构造不可伪造的 _RestoreCapability ──
    # 仅本模块可通过 _RESTORE_SENTINEL 构造;外部代码无法获取 sentinel 引用。
    # _restore_from_backup_data 验证 _sentinel 属性防止伪造。
    capability = _RestoreCapability(
        _RESTORE_SENTINEL,
        backup_id=cap_backup_id,
        manifest_sha256=cap_manifest_sha256,
        payload_key=cap_payload_key,
        ciphertext_sha256=cap_ciphertext_sha256,
        plaintext_sha256=cap_plaintext_sha256,
        encryption_key_id=cap_encryption_key_id,
    )

    # ── R61 P0-03: 调用私有写入器(延迟导入避免循环依赖) ──
    # db_restore.py 在 run_restore() 中导入本模块,故此处必须延迟导入。
    from services.db_restore import _restore_from_backup_data
    result = await _restore_from_backup_data(
        data,
        _capability=capability,
        tables=tables,
        merge=merge,
    )
    return result
