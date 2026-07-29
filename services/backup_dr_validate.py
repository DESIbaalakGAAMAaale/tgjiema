"""R56 §8: 备份灾备与数据可信性 — 三段式备份 + 恢复前验证 + staging 原子切换。

R76 O7 / 10.G: 对象存储后端统一为 S3 兼容协议(Cloudflare R2 生产 / MinIO CI)。
本模块所有 ``r2_storage`` 参数实际为统一 ``R2Storage`` 单例,由
``configure_storage_from_settings()`` 根据 ``OBJECT_STORAGE_BACKEND`` 注入。

报告 §8 要求:
    - SQLite/对象存储 备份采用 ``payload.enc → manifest.json → COMPLETE``
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

import copy as _copy
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets as _secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from loguru import logger

from services.error_codes import AppError, ErrorCodes
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


@dataclass(frozen=True)
class ExactBackupContract:
    """显式对象 key 绑定后的只读备份合同。

    该对象只证明 COMPLETE/manifest/payload 信任链和解密明文有效，不能授权写入。
    写入目标必须由调用方另行建立隔离身份并执行空库断言。
    """

    valid: bool
    backup_id: str = ""
    payload_key: str = ""
    manifest_key: str = ""
    complete_key: str = ""
    manifest_sha256: str = ""
    ciphertext_sha256: str = ""
    plaintext_sha256: str = ""
    schema_version: str = ""
    source_sha: str = ""
    source_database_identity: str = ""
    schema_fingerprint: str = ""
    manifest: Mapping = field(default_factory=lambda: MappingProxyType({}))
    plaintext_bytes: bytes = b""
    error_code: str = ""
    error_message: str = ""


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
    # R63 P0-06: 解密后的明文 bytes(由 validate_backup_payload 填充,
    # 供 validate_and_restore_backup_strict 在 data=None 时使用,避免调用方预加载)
    plaintext_bytes: bytes = b""


# ── R61 P0-03 / R62 P0-01: 不可伪造的恢复能力令牌 ──────────────


# 模块私有 sentinel — 外部模块无法 import 或访问此对象。
# _RestoreCapability.__init__ 仅在 sentinel is _RESTORE_SENTINEL 时允许构造,
# 因此只有 backup_dr_validate.py 内部代码(即 validate_and_restore_backup_strict
# 与 _restore_preverified_payload)能创建合法的 _RestoreCapability 实例。
_RESTORE_SENTINEL = object()


# R63 P1-01: nonce 持久化消费(替代 R62 P0-01 的进程内 _CONSUMED_NONCES set)。
# 原 _CONSUMED_NONCES 为进程内 set,多实例 / 重启 / worker 切换后状态丢失,
# 无法跨进程防重放。现已改为权威 SQLite/CRDB 表 ``restore_capability_nonces``
# 原子消费(详见 database.cache_store.CacheStore.consume_capability_nonce)。
# 审计要求:Python 下划线不是访问控制,真正安全性应来自完整密码/状态验证,
# 而非"外部无法访问 sentinel"的注释。


class _RestoreCapability:
    """R61 P0-03 / R62 P0-01: 不可伪造的恢复能力令牌(增强版)。

    仅 validate_and_restore_backup_strict() / _restore_preverified_payload()
    通过 _RESTORE_SENTINEL 可构造实例。私有写入器
    services.db_restore._restore_from_backup_data 仅接受此类型,
    并在首条语句调用 capability.assert_valid(...) 验证有效性。

    安全模型:
        - _RESTORE_SENTINEL 是模块私有对象(以 _ 前缀标记,且不导出),
          外部代码无法获取它的引用。
        - _RestoreCapability.__init__ 检查 sentinel is _RESTORE_SENTINEL,
          若不匹配则抛 **RuntimeError**(R62 P0-01: 不可伪造令牌被外部构造尝试 —
          属编程契约违反,非业务错误,故用 RuntimeError 而非 AppError),
          阻止外部构造。
        - 因此,只有 backup_dr_validate.py 内部代码能构造合法实例。
        - _restore_from_backup_data 进一步在首条语句调用 assert_valid(payload_digest,
          clock, expected_scope),验证令牌有效性 + 防重放 + payload 一致性 + scope 一致性。

    R62 P0-01 增强(相对 R61):
        - 构造时改用 RuntimeError(非 AppError) — 外部构造为编程契约违反,非业务错误
        - 新增 issuer / nonce / schema_fingerprint / payload_digest 字段
        - 新增 assert_valid(payload_digest, clock, expected_scope) 方法 — 强制断言 API
        - 字段改为只读 property getter(__slots__ 防止新增属性,
          property getter 防止字段被赋值篡改)
        - nonce 防重放:每次成功 assert_valid() 将 nonce 原子消费到权威
          SQLite/CRDB 表 ``restore_capability_nonces``(R63 P1-01 替代原进程内
          ``_CONSUMED_NONCES`` set),二次调用同一 capability 即抛 AppError(防重放攻击)

    令牌字段(来自严格验证通过的 COMPLETE marker / manifest / payload):
        - backup_id:          备份 ID
        - manifest_sha256:    manifest 原始 bytes 的 SHA-256
        - payload_key:        payload.enc 的 R2 key
        - ciphertext_sha256:  密文的 SHA-256
        - plaintext_sha256:   明文的 SHA-256
        - encryption_key_id:  加密密钥 ID
        - issuer:             签发者标识(如 "validate_and_restore_backup_strict" /
                              "BackupEngine._restore_internal"),用于审计
        - nonce:             令牌唯一随机数(secrets.token_hex(16)),防重放
        - schema_fingerprint: schema 指纹(如 manifest.schema_version),用于 scope 校验
        - payload_digest:     payload 内容的 SHA-256(canonical JSON),
                              与 VerifiedBackupPayload.payload_digest 绑定
        - created_at:         令牌构造时间(unix 秒)
        - expires_at:         令牌过期时间戳(unix 秒);过期后 assert_valid 抛 AppError
    """

    # R62 P0-01: __slots__ 防止新增属性,所有字段以 _ 前缀私有 + property getter
    __slots__ = (
        "_sentinel", "_backup_id", "_manifest_sha256", "_payload_key",
        "_ciphertext_sha256", "_plaintext_sha256", "_encryption_key_id",
        "_created_at", "_expires_at", "_issuer", "_nonce",
        "_schema_fingerprint", "_payload_digest",
    )

    def __init__(
        self,
        sentinel,
        *,
        backup_id: str,
        manifest_sha256: str,
        payload_key: str,
        ciphertext_sha256: str,
        plaintext_sha256: str,
        encryption_key_id: str,
        issuer: str,
        schema_fingerprint: str,
        payload_digest: str,
        ttl_seconds: int = 600,
    ):
        # R62 P1-04: data-integrity 域零容忍,协议化为 AppError
        from services.error_codes import AppError, ErrorCodes

        # R62 P0-01: 仅当调用方持有模块私有 _RESTORE_SENTINEL 时允许构造。
        # 不匹配时抛 AppError(信任链违反,data-integrity 域零容忍)。
        if sentinel is not _RESTORE_SENTINEL:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # R62 P0-01: 构造时校验必填字段(空字符串或非法格式即拒绝,防 fail-open)
        # R62 P1-04: data-integrity 域零容忍,协议化为 AppError
        if not backup_id:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if not issuer:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if not schema_fingerprint:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if not payload_digest or len(payload_digest) != 64:
            raise RuntimeError(
                f"_RestoreCapability: payload_digest 必须为 64 hex 字符(实际: {len(payload_digest)})"
            )
        # 6 个摘要/key 字段必须为 64 hex 或合法字符串
        _HEX64 = "0123456789abcdef"
        for field_name, value in (
            ("manifest_sha256", manifest_sha256),
            ("ciphertext_sha256", ciphertext_sha256),
            ("plaintext_sha256", plaintext_sha256),
        ):
            if len(value) != 64 or not all(c in _HEX64 for c in value.lower()):
                raise RuntimeError(
                    f"_RestoreCapability: {field_name} 必须为 64 hex 字符(实际: len={len(value)})"
                )
        if not payload_key:
            # R62 P1-04: data-integrity 域零容忍,协议化为 AppError
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        if not encryption_key_id:
            # R62 P1-04: data-integrity 域零容忍,协议化为 AppError
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # R62 P0-01: nonce 使用 secrets.token_hex(16) — 32 hex 字符,加密强随机
        import time as _time
        self._sentinel = sentinel
        self._backup_id = backup_id
        self._manifest_sha256 = manifest_sha256
        self._payload_key = payload_key
        self._ciphertext_sha256 = ciphertext_sha256
        self._plaintext_sha256 = plaintext_sha256
        self._encryption_key_id = encryption_key_id
        self._issuer = issuer
        self._nonce = _secrets.token_hex(16)
        self._schema_fingerprint = schema_fingerprint
        self._payload_digest = payload_digest
        self._created_at = _time.time()
        self._expires_at = self._created_at + ttl_seconds

    # ── R62 P0-01: 只读 property getter(__slots__ 防止新增属性,
    #    property 防止字段被赋值篡改 — 字段为只读) ──

    @property
    def backup_id(self) -> str:
        return self._backup_id

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    @property
    def payload_key(self) -> str:
        return self._payload_key

    @property
    def ciphertext_sha256(self) -> str:
        return self._ciphertext_sha256

    @property
    def plaintext_sha256(self) -> str:
        return self._plaintext_sha256

    @property
    def encryption_key_id(self) -> str:
        return self._encryption_key_id

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def nonce(self) -> str:
        return self._nonce

    @property
    def schema_fingerprint(self) -> str:
        return self._schema_fingerprint

    @property
    def payload_digest(self) -> str:
        return self._payload_digest

    @property
    def created_at(self) -> float:
        return self._created_at

    @property
    def expires_at(self) -> float:
        return self._expires_at

    async def assert_valid(
        self,
        payload_digest: str,
        clock: float,
        expected_scope: str,
        store=None,
    ) -> None:
        """R62 P0-01 / R63 P1-01: 断言令牌有效 — 失败即抛 AppError(fail-closed)。

        校验维度(全部通过才返回;任一失败即抛 AppError):
            1. sentinel 匹配(令牌由本模块签发,非伪造)
            2. nonce 未被消费(防重放 — 同一令牌只能 assert_valid 一次)
            3. clock <= expires_at(令牌未过期)
            4. payload_digest 与令牌内嵌 digest 一致(防 payload 篡改/替换)
            5. expected_scope 与令牌 schema_fingerprint 一致(防 scope 跨越)
            6. 原子消费 nonce — INSERT OR IGNORE CAS,失败即抛 AppError

        R63 P1-01: nonce 持久化到权威 SQLite/CRDB 表 ``restore_capability_nonces``,
        替代原进程内 ``_CONSUMED_NONCES`` set。原子消费保证多实例/重启/worker 切换后
        仍能防重放。绑定字段(nonce + backup_id + manifest_sha256 + payload_digest)
        作为审计键,即使伪造 nonce 也会因绑定字段不一致被审计捕获。

        成功后:nonce 已被原子消费,二次调用同一令牌即抛 AppError。

        Args:
            payload_digest: 调用方计算出的 VerifiedBackupPayload.payload_digest
            clock: 当前时钟(unix 秒,由调用方传入便于测试)
            expected_scope: 期望的 schema_fingerprint(如当前代码 schema 版本)
            store: 可选 CacheStore 实例(用于测试注入);默认通过
                   ``database.cache_store.get_cache_store()`` 获取单例

        Raises:
            AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): 任一校验失败
        """
        from services.error_codes import AppError, ErrorCodes

        # 1. sentinel 匹配(令牌由本模块签发)
        if self._sentinel is not _RESTORE_SENTINEL:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # 2. nonce 防重放预检(早期拒绝已被消费的 nonce,非安全边界,优化路径)
        # R63 P1-01: 预检与消费之间存在 TOCTOU 窗口,真正的安全边界由
        # consume_capability_nonce 的 INSERT OR IGNORE CAS 保证。
        _store = store
        if _store is None:
            try:
                from database.cache_store import get_cache_store
                _store = get_cache_store()
            except Exception:
                _store = None
        if _store is not None:
            try:
                _already = await _store.is_capability_nonce_consumed(self._nonce)
            except Exception:
                _already = False
            if _already:
                raise AppError(
                    ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                    params={"reason": "nonce_already_consumed"},
                )

        # 3. 时钟过期检查
        if clock > self._expires_at:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # 4. payload_digest 一致性(防 payload 篡改/替换)
        if payload_digest != self._payload_digest:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # 5. scope 一致性(防 schema 跨越攻击)
        if expected_scope != self._schema_fingerprint:
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

        # 6. 原子预留 nonce — INSERT CAS(R63 P1-01 / R64 P1-02)
        # R64 P1-02: assert_valid 改为 reserve(不再直接 consume)。
        # nonce 状态机: reserved → consumed | failed
        #   - assert_valid 调用 reserve_capability_nonce(status='reserved')
        #   - writer 在 restore 成功后 consume_capability_nonce(reserved→consumed)
        #   - writer 在 restore 失败后 fail_capability_nonce(reserved→failed)
        # 多实例/并发安全:rowcount==1 表示本调用方赢得竞态;
        # rowcount==0 表示 nonce 已存在(reserved/consumed/failed,重放攻击或竞态失败)。
        # reserved_by: hostname:pid 用于审计(追踪哪个 worker 预留了 nonce)。
        _reserved_by = ""
        try:
            import socket as _socket
            _reserved_by = f"{_socket.gethostname()}:{os.getpid()}"
        except Exception:
            _reserved_by = f"pid:{os.getpid()}"

        if _store is not None:
            try:
                _won = await _store.reserve_capability_nonce(
                    self._nonce,
                    operation_id=f"{self._backup_id}:{self._issuer}",
                    backup_id=self._backup_id,
                    manifest_sha256=self._manifest_sha256,
                    payload_digest=self._payload_digest,
                    reserved_by=_reserved_by,
                )
            except Exception:
                _won = False
            if not _won:
                # 预留失败 — nonce 已存在(重放或竞态失败)
                raise AppError(
                    ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                    params={"reason": "nonce_already_reserved_or_consumed"},
                )

    def is_valid(self) -> bool:
        """检查能力令牌是否仍有效(向后兼容接口,不消费 nonce)。

        R61 P0-03 原始接口,保留向后兼容。
        新代码应使用 assert_valid()(严格断言 + 防重放)。
        """
        import time as _time
        if self._sentinel is not _RESTORE_SENTINEL:
            return False
        if _time.time() > self._expires_at:
            return False
        # 所有关键信任链字段必须非空
        return all([
            self._backup_id,
            self._manifest_sha256,
            self._payload_key,
            self._ciphertext_sha256,
            self._plaintext_sha256,
            self._issuer,
            self._schema_fingerprint,
            self._payload_digest,
        ])

    def __repr__(self) -> str:
        return (
            f"_RestoreCapability(backup_id={self._backup_id!r}, "
            f"issuer={self._issuer!r}, "
            f"valid={self.is_valid()})"
        )


# ── R62 P0-02 / R63 P0-02: VerifiedBackupPayload(深冻结 dataclass) ─────────


def _deep_freeze(obj):
    """R63 P0-02: 深拷贝 + 递归冻结 Python 对象。

    将 dict 递归转换为 ``MappingProxyType``(只读映射),将 list 转换为 ``tuple``,
    标量(str/int/float/bool/None)保持不变。先 ``copy.deepcopy`` 断绝与调用方
    原 dict/list 的所有引用(含嵌套),再递归冻结,确保调用方无法通过别名引用
    在验证后、写入前篡改 ``VerifiedBackupPayload.tables`` / ``payload``。

    防护维度:
        - 嵌套 dict/list:递归冻结到叶节点
        - 别名引用:deepcopy 断绝共享引用
        - 顶层替换:object.__setattr__ 由 writer 端重算 digest 兜底

    Args:
        obj: 任意 Python 对象(通常为 dict/list/标量)

    Returns:
        冻结后的对象(dict→MappingProxyType, list→tuple, 标量不变)
    """
    # 先深拷贝断绝与调用方原对象的引用(含嵌套可变对象)
    obj = _copy.deepcopy(obj)
    return _freeze_recursive(obj)


def _freeze_recursive(obj):
    """递归冻结辅助:dict→MappingProxyType, list→tuple, 标量不变。"""
    if isinstance(obj, dict):
        return MappingProxyType({k: _freeze_recursive(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_freeze_recursive(item) for item in obj)
    # 标量(str/int/float/bool/None)本身不可变,直接返回
    return obj


def _to_serializable(obj):
    """R63 P0-02: 将深冻结结构还原为 JSON 可序列化的普通 dict/list。

    ``MappingProxyType`` 与 ``tuple`` 不能被 ``json.dumps`` 直接序列化
    (mappingproxy 会落入 default=str 退化为字符串),需先转回普通 dict/list
    再做 canonical JSON 序列化。此函数仅在 digest 计算时调用,不影响
    ``VerifiedBackupPayload`` 字段的不可变性。

    Args:
        obj: 深冻结对象(MappingProxyType / tuple / 标量)

    Returns:
        JSON 可序列化对象(dict / list / 标量)
    """
    if isinstance(obj, MappingProxyType):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_to_serializable(item) for item in obj]
    return obj


def _is_iso8601(value) -> bool:
    """R65 P1-06: 判断字符串是否为合法 ISO 8601 时间戳。

    兼容 ISO 8601 标准格式(含 ``Z`` 后缀或时区偏移),
    使用 ``datetime.fromisoformat`` 解析(Python 3.11+ 已支持 ``Z`` 后缀,
    但为兼容旧版 Python,手动将 ``Z`` 替换为 ``+00:00``)。

    Args:
        value: 待校验值(任意类型,非 str 立即返回 False)

    Returns:
        True 若为合法 ISO 8601 字符串;False 否则
    """
    if not isinstance(value, str) or not value:
        return False
    candidate = value
    # 兼容 Z 后缀(UTC 时区)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    # R65 P1-04: 观测性 — P1-5 规则3 禁止 except 块中裸 return False,
    # 改用"先尝试解析,失败后由 except 块置 flag,函数末尾统一返回"模式。
    parsed_ok = False
    parse_err: Exception | None = None
    try:
        _dt.datetime.fromisoformat(candidate)
        parsed_ok = True
    except (ValueError, TypeError) as exc:
        parse_err = exc
    if not parsed_ok:
        logger.debug(
            _i18n_t(
                "diagnostics.r65.backup_dr_validate.iso8601_check_failed",
                value=repr(value),
                reason=parse_err,
            )
        )
    return parsed_ok


@dataclass(frozen=True)
class VerifiedBackupPayload:
    """R62 P0-02 / R63 P0-02 / R64 P1-01: 已通过严格验证的备份 payload(深冻结不可变数据载体)。

    由 validate_and_restore_backup_strict() 或 _restore_preverified_payload()
    在严格三段式验证通过后构造。作为 _restore_from_backup_data() 的输入,
    替代原 raw data: dict 参数。

    安全保证(R64 P1-01 增强):
        - **单一 canonical bytes 来源(R64 P1-01)**: ``canonical_payload_bytes`` 是
          已验证的原始明文 canonical JSON bytes。``payload`` 与 ``tables`` 不再是
          独立字段,而是从同一 ``canonical_payload_bytes`` 解码的只读 view(property),
          消除 ``tables`` 与 ``payload`` 语义分叉风险。
        - frozen=True:顶层字段不可修改(防止在验证后、写入前被篡改)
        - ``payload`` / ``tables`` property 每次从不可变 ``canonical_payload_bytes``
          (bytes 本身不可变)解码 + ``_deep_freeze`` 返回 ``MappingProxyType`` 只读 view。
        - payload_digest = sha256(canonical_payload_bytes),由 __post_init__ 自动计算。
        - **writer 端重算**: _restore_from_backup_data 首条语句对
          ``verified_payload.canonical_payload_bytes`` 重新计算 SHA-256,
          与 capability.payload_digest 比对。即使 object.__setattr__ 绕过冻结
          替换了 canonical_payload_bytes,重算 digest 也会与 capability 内嵌(构造时)
          的 digest 不匹配 → fail-closed。

    字段:
        backup_id:                备份 ID(来自 manifest.backup_id)
        manifest_sha256:          manifest 原始 bytes 的 SHA-256(来自 COMPLETE marker 验签)
        plaintext_sha256:         解密后明文的 SHA-256(来自 manifest.plaintext_sha256)
        schema_fingerprint:       schema 指纹(通常为 manifest.schema_version,用于 scope 校验)
        canonical_payload_bytes:  已验证的原始明文 canonical JSON bytes(单一来源)
        payload_digest:           sha256(canonical_payload_bytes),由 __post_init__ 自动计算
                                  (不可由调用方设置)
    """
    backup_id: str
    manifest_sha256: str
    plaintext_sha256: str
    schema_fingerprint: str
    canonical_payload_bytes: bytes
    payload_digest: str = ""

    def __post_init__(self):
        # R65 P1-06: 在计算 SHA-256 之前先执行 7 维构造时强校验
        # 任一校验失败即 raise AppError(BACKUP_PAYLOAD_CANONICAL_INVALID),
        # 拒绝"任意 JSON bytes"被称为 canonical
        self._validate_canonical_payload()
        # R64 P1-01: payload_digest 从 canonical_payload_bytes 计算(sha256)
        # canonical_payload_bytes 是 bytes(不可变),无需深冻结
        if not self.payload_digest:
            object.__setattr__(
                self, "payload_digest",
                hashlib.sha256(self.canonical_payload_bytes).hexdigest(),
            )

    def _validate_canonical_payload(self) -> None:
        """R65 P1-06: VerifiedBackupPayload 构造时 7 维强校验(fail-closed)。

        校验维度(任一失败即 raise AppError,不计算 SHA-256):
            1. canonical_payload_bytes 必须为 bytes(拒绝 str/int/None/list)
            2. UTF-8 可解码(拒绝非法 UTF-8 bytes)
            3. JSON object(拒绝 array/primitive/null)
            4. 任意层级无重复 key(object_pairs_hook=list 检测)
            5. schema: version(int≥1) / backup_id(非空 str) /
               created_at(ISO 8601 str) / tables(dict) 必填且类型正确
            6. tables 中每个表名对应的值必须为 list
            7. canonical round-trip bytes 完全相等
               (sort_keys + separators=(",",":") + ensure_ascii=False + allow_nan=False)

        Raises:
            AppError(BACKUP_PAYLOAD_CANONICAL_INVALID): 任一校验失败
        """
        from services.error_codes import AppError, ErrorCodes

        def _reject(reason: str, field: str = "") -> None:
            raise AppError(
                ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID,
                params={"reason": reason, "field": field},
            )

        # ── 1. bytes 类型校验(拒绝 str/int/None/list 等) ──
        if not isinstance(self.canonical_payload_bytes, (bytes, bytearray)):
            _reject(
                f"canonical_payload_bytes 必须为 bytes 类型,实际: "
                f"{type(self.canonical_payload_bytes).__name__}",
                field="canonical_payload_bytes",
            )

        raw_bytes = bytes(self.canonical_payload_bytes)

        # ── 2. UTF-8 可解码校验 ──
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            _reject(f"canonical_payload_bytes 不是合法 UTF-8: {e}",
                    field="canonical_payload_bytes")

        # ── 3 & 4. JSON object 校验 + 任意层级无重复 key 校验 ──
        # object_pairs_hook 保留所有 (key, value) 对(包括重复 key),
        # 在 hook 内手动检测每个 dict 层级是否有重复 key,有则抛 ValueError。
        def _detect_duplicate_pairs(pairs):
            seen = set()
            for k, _ in pairs:
                if k in seen:
                    raise ValueError(f"duplicate key: {k}")
                seen.add(k)
            return dict(pairs)

        try:
            data = json.loads(text, object_pairs_hook=_detect_duplicate_pairs)
        except ValueError as e:
            # 重复 key 检测触发(ValueError)
            _reject(f"JSON 含重复 key: {e}", field="canonical_payload_bytes")
        except json.JSONDecodeError as e:
            _reject(f"canonical_payload_bytes 不是合法 JSON: {e}",
                    field="canonical_payload_bytes")

        # ── 3. JSON object 校验(拒绝 array/primitive/null) ──
        if not isinstance(data, dict):
            _reject(
                f"canonical payload 必须为 JSON object,实际: "
                f"{type(data).__name__}",
                field="canonical_payload_bytes",
            )

        # ── 5. schema 校验:version / backup_id / created_at / tables ──
        # 5.1 version 必须为 int(≥1),且不能是 bool(Python 中 bool 是 int 子类)
        if "version" not in data:
            _reject("缺少必填字段: version", field="version")
        _version = data["version"]
        if isinstance(_version, bool) or not isinstance(_version, int):
            _reject(
                f"version 必须为 int,实际类型: {type(_version).__name__}",
                field="version",
            )
        if _version < 1:
            _reject(f"version 必须 ≥ 1,实际: {_version}", field="version")

        # 5.2 backup_id 必须为非空 str
        if "backup_id" not in data:
            _reject("缺少必填字段: backup_id", field="backup_id")
        _backup_id = data["backup_id"]
        if not isinstance(_backup_id, str):
            _reject(
                f"backup_id 必须为 str,实际类型: {type(_backup_id).__name__}",
                field="backup_id",
            )
        if not _backup_id:
            _reject("backup_id 不能为空字符串", field="backup_id")

        # 5.3 created_at 必须为合法 ISO 8601 字符串
        if "created_at" not in data:
            _reject("缺少必填字段: created_at", field="created_at")
        _created_at = data["created_at"]
        if not isinstance(_created_at, str):
            _reject(
                f"created_at 必须为 ISO 8601 字符串,实际类型: "
                f"{type(_created_at).__name__}",
                field="created_at",
            )
        if not _is_iso8601(_created_at):
            _reject(f"created_at 不是合法 ISO 8601 时间戳: {_created_at}",
                    field="created_at")

        # 5.4 tables 必须为 dict
        if "tables" not in data:
            _reject("缺少必填字段: tables", field="tables")
        _tables = data["tables"]
        if not isinstance(_tables, dict):
            _reject(
                f"tables 必须为 dict,实际类型: {type(_tables).__name__}",
                field="tables",
            )

        # ── 6. tables 值类型校验(每个表名对应值必须为 list) ──
        for table_name, rows in _tables.items():
            if not isinstance(rows, list):
                _reject(
                    f"tables.{table_name} 必须为 list,实际类型: "
                    f"{type(rows).__name__}",
                    field=f"tables.{table_name}",
                )

        # ── 7. canonical round-trip bytes 完全相等校验 ──
        # 用 _canonical_json_bytes 重新序列化,要求 bytes 完全相等
        # (sort_keys + separators=(",",":") + ensure_ascii=False + allow_nan=False)
        try:
            re_canonical = json.dumps(
                data, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as e:
            _reject(f"canonical round-trip 序列化失败: {e}",
                    field="canonical_payload_bytes")

        if re_canonical != raw_bytes:
            _reject(
                "canonical_payload_bytes 不是 canonical 形式"
                "(sort_keys + 紧凑分隔符 + ensure_ascii=False + allow_nan=False)",
                field="canonical_payload_bytes",
            )

    @property
    def payload(self) -> MappingProxyType:
        """从 canonical_payload_bytes 解码的只读 view(MappingProxyType 深冻结)。

        R64 P1-01: payload 不再是独立字段,而是从 canonical_payload_bytes 解码。
        每次访问重新解码(无缓存),但 canonical_payload_bytes 不可变,保证一致性。
        """
        data = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        return _deep_freeze(data)

    @property
    def tables(self) -> MappingProxyType:
        """从 canonical_payload_bytes 解码的 tables 只读 view(MappingProxyType)。

        R64 P1-01: tables 不再是独立字段,而是从 canonical_payload_bytes 解码。
        与 payload 从同一 bytes 解码,消除语义分叉风险。
        """
        _payload = self.payload
        return _payload.get("tables", _deep_freeze({}))


def _canonical_json_bytes(data) -> bytes:
    """R64 P1-01: 将数据序列化为 canonical JSON bytes(fail-closed)。

    使用 sort_keys=True + separators=(",", ":") + ensure_ascii=False + allow_nan=False,
    保证 canonical 形式且拒绝 NaN/Infinity。

    **fail-closed**: 禁止 default 退化为 str 序列化,对不可序列化类型(bytes/NaN/Infinity/
    自定义对象/set 等)直接 raise ``AppError(BACKUP_PAYLOAD_NOT_SERIALIZABLE)``。
    只允许 JSON schema 声明类型(str/int/float/bool/None/list/dict)。

    Args:
        data: 备份数据(普通 dict 或深冻结 MappingProxyType/tuple)

    Returns:
        canonical JSON bytes(utf-8 编码)

    Raises:
        AppError(BACKUP_PAYLOAD_NOT_SERIALIZABLE): 数据含不可序列化类型
    """
    from services.error_codes import AppError, ErrorCodes
    # R63 P0-02: 深冻结结构需还原为普通 dict/list 才能 JSON 序列化
    serializable = _to_serializable(data) if isinstance(data, (MappingProxyType, tuple)) else data
    try:
        # R64 P1-01: allow_nan=False 拒绝 NaN/Infinity;无 default=str 拒绝 bytes/自定义对象
        return json.dumps(
            serializable, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as e:
        # TypeError: bytes/自定义对象/set 等不可序列化
        # ValueError: NaN/Infinity(allow_nan=False)
        raise AppError(
            ErrorCodes.BACKUP_PAYLOAD_NOT_SERIALIZABLE,
            params={"reason": str(e)},
        )


def _compute_payload_digest(data) -> str:
    """R62 P0-02 / R63 P0-02 / R64 P1-01: 计算备份数据的 SHA-256 digest(canonical JSON,fail-closed)。

    使用 sort_keys=True + separators=(",", ":") + ensure_ascii=False + allow_nan=False,
    保证相同内容不同 key 顺序产生相同 digest(canonical 形式)。

    R64 P1-01 fail-closed: 禁止 default 退化为 str 序列化,对不可序列化类型
    (bytes/NaN/Infinity/自定义对象)直接 raise ``AppError(BACKUP_PAYLOAD_NOT_SERIALIZABLE)``。
    只允许 JSON schema 声明类型。

    此 digest 与 _RestoreCapability.payload_digest 绑定,
    在 _restore_from_backup_data 的首条 assert_valid() 调用中由 writer
    重新计算实际 bytes 的 SHA-256 并与 capability 内嵌 digest 比对,
    防止 payload 在验证后、写入前被替换(含 object.__setattr__ 攻击)。

    Args:
        data: 备份数据(普通 dict 或深冻结 MappingProxyType,通常含 "tables" 键)

    Returns:
        64 字符 hex sha256 digest

    Raises:
        AppError(BACKUP_PAYLOAD_NOT_SERIALIZABLE): 数据含不可序列化类型
    """
    return hashlib.sha256(_canonical_json_bytes(data)).hexdigest()


def _enrich_payload_data(data, *, backup_id: str, created_at: str, version: int = 1):
    """R65 P1-06: 在构造 VerifiedBackupPayload 前补齐 canonical payload 必填字段。

    生产代码 ``backup_data`` 仅含 ``tables`` / ``backup_time``,但 R65 P1-06 要求
    canonical payload 必须含 ``version`` / ``backup_id`` / ``created_at`` / ``tables``
    四个必填字段。本函数在不覆盖已有字段的前提下补齐缺失字段,使构造的
    ``VerifiedBackupPayload`` 能通过 7 维构造时强校验。

    补齐策略(at-minimum,不覆盖):
        - ``version``: 默认 1,若 data 已含则保留原值
        - ``backup_id``: 来自 manifest.backup_id,若 data 已含则保留原值
        - ``created_at``: 来自 manifest.created_at,若 data 已含则保留原值
        - ``tables``: 不补齐(由调用方保证存在,否则 VerifiedBackupPayload 校验拒绝)

    Args:
        data: 原始备份数据(通常为 dict,含 "tables" 键)
        backup_id: 备份 ID(来自 manifest.backup_id)
        created_at: 创建时间 ISO 8601 字符串(来自 manifest.created_at)
        version: canonical payload 版本号(默认 1)

    Returns:
        新 dict(浅拷贝 + 补齐字段);非 dict 输入原样返回(由
        VerifiedBackupPayload 校验拒绝)
    """
    if not isinstance(data, dict):
        return data
    enriched = dict(data)
    if "version" not in enriched:
        enriched["version"] = version
    if "backup_id" not in enriched:
        enriched["backup_id"] = backup_id
    if "created_at" not in enriched:
        enriched["created_at"] = created_at
    return enriched


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
    *,
    expected_complete_key: str = "",
    expected_payload_key: str = "",
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

    complete_key = expected_complete_key or get_complete_key(timestamp, backup_type)
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
        if expected_payload_key and marker_payload_key != expected_payload_key:
            return BackupValidationResult(
                valid=False,
                backup_id=timestamp,
                error_code="BACKUP.RESTORE.COMPLETE_MARKER_INVALID",
                error_message=(
                    f"COMPLETE marker payload_key mismatch: "
                    f"expected={expected_payload_key}, actual={marker_payload_key}"
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
            ciphertext_sha256=marker_payload_sha,  # 已签名 COMPLETE 中的 payload digest
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
    *,
    expected_manifest_key: str = "",
) -> BackupValidationResult:
    """R56 §8: 验证 manifest 字段完整性。

    检查 manifest 包含所有必填字段(ciphertext_sha256、schema_version、
    backup_id、encryption.key_id 等)。
    """
    manifest_key = expected_manifest_key or get_manifest_key(timestamp, backup_type)
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
    *,
    expected_payload_key: str = "",
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
        - AAD 必须与权威 backup_once() 加密格式一致:
          backup_id|schema_version|key_id

    AAD 绑定字段:
        backup_id | schema_version | key_id
        — 与 services.backup_crypto.encrypt_payload() 的实际格式保持一致
        — payload_key、ciphertext/plaintext digest 由已签名 COMPLETE + manifest
          信任链独立强绑定，不能伪造为 AEAD 中并不存在的字段

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

    payload_key = expected_payload_key or get_payload_key(timestamp, backup_type)
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
            # 权威备份 AAD 由 backup_crypto.encrypt_payload() 定义为
            # backup_id|schema_version|key_id。对象 key 与两个 digest 已分别由
            # 已签名 COMPLETE marker 和 manifest 原始 bytes digest 强绑定。
            aad = f"{timestamp}|{schema_version}|{key_id}".encode()
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
            plaintext_bytes=plaintext,  # R63 P0-06: 返回明文 bytes 供 strict 入口使用(data=None 时)
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


def _exact_contract_failure(
    *,
    backup_id: str,
    payload_key: str,
    manifest_key: str,
    complete_key: str,
    error_code: str,
    error_message: str,
) -> ExactBackupContract:
    """构造保留 exact-key 上下文的 fail-closed 结果。"""
    return ExactBackupContract(
        valid=False,
        backup_id=backup_id,
        payload_key=payload_key,
        manifest_key=manifest_key,
        complete_key=complete_key,
        error_code=error_code,
        error_message=error_message,
    )


async def validate_exact_backup_contract(
    *,
    backup_id: str,
    payload_key: str,
    manifest_key: str,
    complete_key: str,
    backup_type: str,
    r2_storage,
    signing_key: bytes,
    current_schema_version: str,
    kek: bytes | None = None,
    payload_read_key: str = "",
) -> ExactBackupContract:
    """验证由 backup-state 显式给出的三对象合同并返回只读明文。

    本入口不按 ``backup_id`` 重新推导任何对象 key。验证顺序固定为：
    COMPLETE 签名与 exact-key 绑定 → manifest 原始 bytes digest → manifest
    exact-key/digest/schema 绑定 → payload 密文 digest → AES-GCM 解密与明文
    digest。``payload_read_key`` 仅用于 corruption negative 下载损坏副本；
    COMPLETE/manifest 仍必须绑定权威 ``payload_key``，因此不能替换信任链。
    任何下载、解析、配置或认证错误都返回其真实错误码，调用方不得将其当作
    corruption negative 成功。
    """
    exact_values = {
        "backup_id": backup_id,
        "payload_key": payload_key,
        "manifest_key": manifest_key,
        "complete_key": complete_key,
        "backup_type": backup_type,
        "current_schema_version": current_schema_version,
    }
    actual_payload_read_key = payload_read_key.strip() or payload_key
    missing = [name for name, value in exact_values.items() if not str(value).strip()]
    if missing or not signing_key or r2_storage is None:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=ErrorCodes.BACKUP_RESTORE_EXACT_CONTRACT_INVALID,
            error_message=f"missing exact contract fields: {missing}",
        )
    if len({payload_key, manifest_key, complete_key}) != 3:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=ErrorCodes.BACKUP_RESTORE_EXACT_CONTRACT_INVALID,
            error_message="payload/manifest/COMPLETE keys must be distinct",
        )

    complete_result = await validate_backup_completeness(
        backup_id,
        backup_type,
        r2_storage,
        expected_manifest_key=manifest_key,
        signing_key=signing_key,
        expected_backup_id=backup_id,
        expected_complete_key=complete_key,
        expected_payload_key=payload_key,
    )
    if not complete_result.valid:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=complete_result.error_code,
            error_message=complete_result.error_message,
        )

    try:
        manifest_bytes = await r2_storage.download(manifest_key)
    except Exception as exc:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=ErrorCodes.BACKUP_RESTORE_MANIFEST_DOWNLOAD_FAILED,
            error_message=f"manifest download failed: {type(exc).__name__}: {exc}",
        )
    if manifest_bytes is None:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.MANIFEST_MISSING",
            error_message=f"manifest.json not found: {manifest_key}",
        )
    actual_manifest_sha = _compute_sha256(manifest_bytes)
    if actual_manifest_sha != complete_result.manifest_sha256:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.MANIFEST_HASH_MISMATCH",
            error_message=(
                "manifest hash mismatch: "
                f"expected={complete_result.manifest_sha256[:16]}..., "
                f"actual={actual_manifest_sha[:16]}..."
            ),
        )
    try:
        manifest = json.loads(manifest_bytes)
    except Exception as exc:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.MANIFEST_INVALID",
            error_message=f"manifest JSON invalid: {type(exc).__name__}: {exc}",
        )
    if not isinstance(manifest, dict):
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.MANIFEST_INVALID",
            error_message="manifest must be a JSON object",
        )

    missing_fields = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing_fields:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.MANIFEST_INCOMPLETE",
            error_message=f"manifest missing required fields: {missing_fields}",
        )

    manifest_backup_id = str(manifest.get("backup_id", ""))
    manifest_payload_key = str(manifest.get("payload_key", ""))
    manifest_manifest_key = str(manifest.get("manifest_key", ""))
    ciphertext_sha = str(manifest.get("ciphertext_sha256", ""))
    plaintext_sha = str(manifest.get("plaintext_sha256", ""))
    schema_version = str(manifest.get("schema_version", ""))
    encryption = manifest.get("encryption", {})
    if manifest_backup_id != backup_id:
        mismatch = f"manifest backup_id mismatch: expected={backup_id}, actual={manifest_backup_id}"
    elif manifest_payload_key != payload_key:
        mismatch = f"manifest payload_key mismatch: expected={payload_key}, actual={manifest_payload_key}"
    elif manifest_manifest_key != manifest_key:
        mismatch = f"manifest manifest_key mismatch: expected={manifest_key}, actual={manifest_manifest_key}"
    elif ciphertext_sha != complete_result.ciphertext_sha256:
        mismatch = "manifest ciphertext_sha256 does not match signed COMPLETE payload_sha256"
    else:
        mismatch = ""
    if mismatch:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=ErrorCodes.BACKUP_RESTORE_MANIFEST_BINDING_MISMATCH,
            error_message=mismatch,
        )
    if not isinstance(encryption, dict) or not encryption.get("encrypted"):
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=ErrorCodes.BACKUP_RESTORE_ENCRYPTION_REQUIRED,
            error_message="authoritative exact backup must use encrypted payload",
        )
    key_id = str(encryption.get("key_id", ""))
    wrapped_dek = str(encryption.get("wrapped_dek", ""))
    nonce = str(encryption.get("nonce", ""))
    digests_valid = all(
        len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())
        for value in (ciphertext_sha, plaintext_sha)
    )
    if not key_id or not wrapped_dek or not nonce or not digests_valid:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.MANIFEST_INVALID",
            error_message="manifest encryption metadata or digests are invalid",
        )
    compatible, reason = validate_schema_compatibility(
        schema_version, current_schema_version
    )
    if not compatible:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.SCHEMA_INCOMPATIBLE",
            error_message=reason,
        )

    try:
        ciphertext = await r2_storage.download(actual_payload_read_key)
    except Exception as exc:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code=ErrorCodes.BACKUP_RESTORE_PAYLOAD_DOWNLOAD_FAILED,
            error_message=f"payload download failed: {type(exc).__name__}: {exc}",
        )
    if ciphertext is None:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.PAYLOAD_MISSING",
            error_message=f"payload.enc not found: {actual_payload_read_key}",
        )
    actual_ciphertext_sha = _compute_sha256(ciphertext)
    if actual_ciphertext_sha != ciphertext_sha:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.CIPHERTEXT_HASH_MISMATCH",
            error_message=(
                "ciphertext hash mismatch: "
                f"expected={ciphertext_sha[:16]}..., actual={actual_ciphertext_sha[:16]}..."
            ),
        )

    try:
        from services.backup_crypto import decrypt_payload

        plaintext = decrypt_payload(
            ciphertext,
            wrapped_dek=wrapped_dek,
            nonce_b64=nonce,
            kek=kek,
            expected_plaintext_sha256=plaintext_sha,
            backup_id=backup_id,
            schema_version=schema_version,
            key_id=key_id,
            allow_legacy_aad=False,
        )
    except Exception as exc:
        return _exact_contract_failure(
            backup_id=backup_id,
            payload_key=payload_key,
            manifest_key=manifest_key,
            complete_key=complete_key,
            error_code="BACKUP.RESTORE.DECRYPT_FAILED",
            error_message=f"decryption failed: {type(exc).__name__}: {exc}",
        )

    return ExactBackupContract(
        valid=True,
        backup_id=backup_id,
        payload_key=payload_key,
        manifest_key=manifest_key,
        complete_key=complete_key,
        manifest_sha256=actual_manifest_sha,
        ciphertext_sha256=actual_ciphertext_sha,
        plaintext_sha256=_compute_sha256(plaintext),
        schema_version=schema_version,
        source_sha=str(manifest.get("source_sha") or manifest.get("commit_sha") or ""),
        source_database_identity=str(manifest.get("source_database_identity", "")),
        schema_fingerprint=str(manifest.get("schema_fingerprint", "")),
        manifest=_deep_freeze(manifest),
        plaintext_bytes=bytes(plaintext),
    )


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
    *,
    expected_complete_key: str = "",
    expected_payload_key: str = "",
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
        expected_complete_key=expected_complete_key,
        expected_payload_key=expected_payload_key,
    )
    if not r1.valid:
        return r1
    # 2. manifest 完整性
    r2 = await validate_backup_manifest(
        timestamp,
        backup_type,
        r2_storage,
        expected_manifest_key=expected_manifest_key,
    )
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
        expected_payload_key=expected_payload_key or r1.payload_key,
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


def _validate_r76_p0_05_env() -> "tuple[bytes, str, int, int]":
    """R76 P0-05: 校验环境变量并返回独立 expected 值来源(必须在备份验证前调用)。

    生产环境(无 ALLOW_LEGACY_RESTORE=1)缺失任一 env → fail-closed(AppError)。
    测试环境(ALLOW_LEGACY_RESTORE=1,conftest autouse fixture)允许回退到默认值。

    必须在备份验证步骤之前调用,确保 env 缺失时立即 fail-closed,
    不会因后续备份验证步骤的失败掩盖 env 校验缺失。

    Returns:
        (_restore_signing_key, _ctx_source_sha, _ctx_run_id, _ctx_run_attempt)
    """
    _restore_signing_key = os.environ.get("RESTORE_CAPABILITY_SIGNING_KEY", "").encode()
    if not _restore_signing_key:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={"reason": "RESTORE_CAPABILITY_SIGNING_KEY not set"},
        )
    _legacy_mode = os.environ.get("ALLOW_LEGACY_RESTORE", "").lower() in ("1", "true", "yes")
    _ctx_source_sha = os.environ.get("GITHUB_SHA")
    if not _ctx_source_sha:
        if _legacy_mode:
            _ctx_source_sha = "local"
        else:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "source_sha required (R76 P0-05)"},
            )
    _ctx_run_id_str = os.environ.get("GITHUB_RUN_ID")
    if _ctx_run_id_str is None:
        if _legacy_mode:
            _ctx_run_id = 0
        else:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "run_id required (R76 P0-05)"},
            )
    else:
        try:
            _ctx_run_id = int(_ctx_run_id_str)
        except (TypeError, ValueError):
            if _legacy_mode:
                _ctx_run_id = 0
            else:
                raise AppError(
                    ErrorCodes.VALIDATION_FAILED,
                    params={"reason": "run_id required (R76 P0-05)"},
                )
    _ctx_run_attempt_str = os.environ.get("GITHUB_RUN_ATTEMPT")
    if _ctx_run_attempt_str is None:
        if _legacy_mode:
            _ctx_run_attempt = 1
        else:
            raise AppError(
                ErrorCodes.VALIDATION_FAILED,
                params={"reason": "run_attempt required (R76 P0-05)"},
            )
    else:
        try:
            _ctx_run_attempt = int(_ctx_run_attempt_str)
        except (TypeError, ValueError):
            if _legacy_mode:
                _ctx_run_attempt = 1
            else:
                raise AppError(
                    ErrorCodes.VALIDATION_FAILED,
                    params={"reason": "run_attempt required (R76 P0-05)"},
                )
    return _restore_signing_key, _ctx_source_sha, _ctx_run_id, _ctx_run_attempt


async def validate_and_restore_backup_strict(
    *,
    data: "dict | None" = None,          # R63 P0-06: 可选 — None 时由 strict service 自行解密 payload
    tables: "list[str] | None" = None,
    merge: bool = False,
    # 严格三段式验证参数(全部必填 — R62 P0-01: 移除 skip 模式,无绕过路径)
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
) -> dict:
    """R59 P0-04 / R61 P0-03 / R62 P0-01 / R63 P0-06: 统一 fail-closed 备份恢复公共入口 — 整合验证 + 写入。

    本函数是生产恢复的**唯一公共写入入口**(无 skip/override 参数,无绕过路径)。
    db_restore.py / db_backup.py / backup_engine.py / disaster_recovery.py 必须通过
    本函数执行恢复写入,禁止直接调用 services.db_restore._restore_from_backup_data(私有)。

    R61 P0-03 信任链整改:
        - 本函数是**唯一**能构造 _RestoreCapability 的公共入口
          (sentinel _RESTORE_SENTINEL 为模块私有,外部代码无法构造合法令牌)。
        - 构造令牌后调用私有写入器 _restore_from_backup_data(verified_payload,
          _capability=cap),写入器在首条语句调用 capability.assert_valid() 验证有效性。
        - 旧 R59 P0-04 / R60 P0-03 的 BackupValidationResult 信任令牌已废弃
          (其为公开 dataclass,任意调用方可构造 valid=True,无法防止伪造)。

    R62 P0-01 信任链收紧(本次审计整改):
        - **彻底移除 skip_strict_validation / validation_note / 6 个 *_override 参数**,
          禁止任何形式的绕过(原"兼容模式"为安全漏洞,允许调用方跳过
          manifest/ciphertext/plaintext/key 校验)。
        - 旧格式备份(db_backup_*.json 单文件)必须 FAIL,错误消息指向离线导入/迁移工具。
        - BackupEngine._restore_internal 等已通过等效验证的内部代码路径
          使用新的 _restore_preverified_payload() 内部辅助函数(仍由 sentinel 保护,
          且必须提供 VerifiedBackupPayload + 完整信任链元数据)。

    R63 P0-06 CLI 三段式发现模型:
        - ``data`` 参数改为可选(默认 None)。当 ``data=None`` 时,本函数从
          ``validate_backup_payload`` 返回的解密明文 ``plaintext_bytes`` 解析数据,
          调用方不得预加载/拼装 data。CLI ``run_restore`` 仅传入 backup_id 与
          三段式验证参数,由本函数自行完成 COMPLETE→manifest→payload 发现与解密。
        - 当 ``data`` 非空时(向后兼容 R62 测试),使用调用方提供的数据,但仍走
          完整三段式验证(下载/解密/校验)以保证信任链完整。

    严格三段式验证(强制,无 skip 路径):
        1. 下载 COMPLETE → 验签 → 比对 backup_id
        2. 下载 manifest 原始 bytes → 比对 SHA256(manifest_bytes)
        3. 解析严格 schema → 比对 payload_key
        4. 下载密文 → 比对密文 SHA
        5. AEAD 解密并验证 AAD → 比对明文 SHA
        6. 数据库完整性检查(若提供 sqlite_db_staging)
        7. 临时文件 fsync → 原子替换 → 父目录 fsync(若提供 staging/final path)
        全部通过后,构造 VerifiedBackupPayload + _RestoreCapability 并调用私有写入器。

    AAD 绑定字段(R59 P0-04):
        backup_id | schema_version | payload_key | key_id | plaintext_sha256

    Args:
        data: R63 P0-06 可选 — 待写入的备份数据 dict(含 "tables" 键)。
              None 时由本函数从解密明文解析(推荐路径,调用方不预加载)。
        tables: 仅恢复指定表;None 则恢复备份中的所有表
        merge: True=增量补充;False=覆盖(默认)
        timestamp: 备份 ID(timestamp) — 必填
        backup_type: full / incremental(默认 full)
        r2_storage: R2 存储客户端 — 必填
        signing_key: COMPLETE marker 签名密钥 — 必填
        decryptor: 解密器对象(需提供 decrypt(ciphertext, aad) -> plaintext) — 必填
        expected_manifest_key: 期望的 manifest R2 key — 必填
        expected_backup_id: 期望的 backup_id — 必填
        current_schema_version: 当前 _BACKUP_SCHEMA_VERSION(schema 兼容性检查)
        staging_path: staging 临时文件路径(可选,提供时执行原子切换)
        final_path: 最终目标路径(可选,提供时执行原子切换)
        sqlite_db_staging: SQLite DB staging 路径(可选,提供时执行 integrity_check)

    Returns:
        dict: _restore_from_backup_data 的结果
              {"restored": {table: rows}, "skipped": [tables], "errors": [msgs]}

    Raises:
        AppError: 严格三段式验证失败时(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)。
                  旧格式备份(db_backup_*.json)应**不调用本函数** —
                  调用方必须在调用前检测并 FAIL,提示用户使用离线导入/迁移工具。
    """
    # R76 §10.M: 在函数体开头无条件导入 AppError/ErrorCodes,避免下方 20+ 处
    # 条件分支内的 local import 导致 Python 将 AppError 视为整个函数的局部变量,
    # 当条件分支跳过 import 时触发 UnboundLocalError(模块级 line 45 已导入,
    # 此处为防御性绑定,确保所有代码路径都能引用)
    from services.error_codes import AppError, ErrorCodes

    # R62 P0-01: 信任链元数据(由严格三段式验证填充)
    cap_backup_id = ""
    cap_manifest_sha256 = ""
    cap_payload_key = ""
    cap_plaintext_sha256 = ""
    cap_schema_fingerprint = ""

    # ── 严格三段式验证模式(无 skip 路径) ──
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

    # R76 P0-05: env 校验必须在备份验证前执行 — 缺失即 fail-closed,
    # 不得因后续备份验证步骤的失败掩盖 env 校验缺失
    _restore_signing_key, _ctx_source_sha, _ctx_run_id, _ctx_run_attempt = (
        _validate_r76_p0_05_env()
    )

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
    cap_plaintext_sha256 = pt_sha
    # R62 P0-01: schema_fingerprint 用作 scope(防 schema 跨越攻击)
    cap_schema_fingerprint = schema_version

    # ── R63 P0-06: 当 data=None 时,从解密明文解析备份数据(调用方不预加载) ──
    # strict service 自行完成 COMPLETE→manifest→payload 发现与解密,
    # 调用方(CLI run_restore)只传入 backup_id 与三段式验证参数。
    if data is None:
        if not r5.plaintext_bytes:
            from services.error_codes import AppError, ErrorCodes
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)
        try:
            data = json.loads(r5.plaintext_bytes.decode("utf-8"))
        except Exception as e:
            from services.error_codes import AppError, ErrorCodes
            logger.error(
                _i18n_t(
                    'services.backup_dr_validate.logger_manifest_json_parse_failed',
                    e=e,
                )
            )
            raise AppError(ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED)

    # ── R62 P0-02 / R63 P0-02 / R64 P1-01: 构造 VerifiedBackupPayload(深冻结,不可篡改) ──
    # R64 P1-01: 单一 canonical bytes 来源 — payload/tables 改为 property,
    # 从 canonical_payload_bytes 解码,消除 tables 与 payload 语义分叉风险。
    # R65 P1-06: 用 _enrich_payload_data 补齐 canonical payload 必填字段
    # (version/backup_id/created_at/tables),使构造时强校验通过
    cap_created_at = str(manifest.get("created_at", "")) if isinstance(manifest, dict) else ""
    enriched_data = _enrich_payload_data(
        data, backup_id=cap_backup_id, created_at=cap_created_at,
    )
    verified_payload = VerifiedBackupPayload(
        backup_id=cap_backup_id,
        manifest_sha256=cap_manifest_sha256,
        plaintext_sha256=cap_plaintext_sha256,
        schema_fingerprint=cap_schema_fingerprint,
        canonical_payload_bytes=_canonical_json_bytes(enriched_data),
    )

    # ── R75 P0-06: 签发 capability dict(替代旧 _RestoreCapability 对象) ──
    # 新的 verify_and_consume_capability() 需要 HMAC 签名的 capability dict,
    # 不再使用 sentinel 保护的 _RestoreCapability 对象。
    from services.restore_capability_file import issue_capability
    # R76 P0-05: _restore_signing_key / _ctx_source_sha / _ctx_run_id /
    # _ctx_run_attempt 已在函数开头通过 _validate_r76_p0_05_env() 校验获取
    _ctx_audience = "backup_dr_validate"
    _ctx_target_uri = f"sqlite://{cap_payload_key}"
    # R76 P0-05: operation_id 由 orchestrator 生成;此处使用 backup_id+timestamp
    # 作为确定性 operation_id(backup_dr_validate 路径无 orchestrator)
    _ctx_operation_id = f"restore-{cap_backup_id}-{timestamp}"
    _ctx_nonce = _secrets.token_hex(16)
    capability = issue_capability(
        backup_id=cap_backup_id,
        source_sha=_ctx_source_sha,
        target_database_identity=cap_schema_fingerprint,
        target_path=cap_payload_key,
        run_id=_ctx_run_id,
        run_attempt=_ctx_run_attempt,
        audience=_ctx_audience,
        target_uri=_ctx_target_uri,
        signing_key=_restore_signing_key,
        operation_id=_ctx_operation_id,
        nonce=_ctx_nonce,
    )

    # R76 P0-05 / O8: 构造 RestoreOperationContext(独立 expected 值来源)
    # context 字段与 issue_capability 使用相同的独立来源(env + manifest),
    # 但 context 作为独立对象传递给 writer,不依赖 capability 自身回填
    from services.restore_nonce_store import RestoreNonceStore
    from services.restore_operation_context import RestoreOperationContext
    operation_context = RestoreOperationContext(
        operation_id=_ctx_operation_id,
        backup_id=cap_backup_id,
        source_sha=_ctx_source_sha,
        run_id=_ctx_run_id,
        run_attempt=_ctx_run_attempt,
        audience=_ctx_audience,
        target_identity=cap_schema_fingerprint,
        target_uri=_ctx_target_uri,
        manifest_digest=cap_manifest_sha256,
        payload_digest=cap_plaintext_sha256,
        allowed_action="restore_to_blank_target",
        nonce=_ctx_nonce,
    )
    operation_context.validate()  # fail-closed

    # R76 P0-06 / O8: 获取 RestoreNonceStore(数据库 CAS,替代 /tmp 文件 CAS)
    # 当 get_cache_store() 返回 None(单元测试场景)时,跳过 nonce 预留,
    # nonce_store=None 传给 writer,writer 的 verify_and_consume_capability
    # 会在 None.consume 上抛异常(fail-closed),由集成测试覆盖真实 DB 路径
    try:
        from database.cache_store import get_cache_store
        _cache_store = get_cache_store()
        if _cache_store is None:
            logger.debug(
                "[backup_dr_validate] R76 O8: cache_store 不可用,"
                "跳过 nonce 预留(测试场景)"
            )
            nonce_store = None
        else:
            nonce_store = RestoreNonceStore(_cache_store)
            # 预留 nonce(数据库 CAS)
            reserved = await nonce_store.reserve(
                capability, operation_context,
                reserved_by=f"backup_dr_validate:{_ctx_operation_id}",
            )
            if not reserved:
                raise AppError(
                    ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                    params={
                        "reason": "nonce_reserve_failed_replay_detected",
                        "operation_id": _ctx_operation_id,
                    },
                )
    except AppError:
        raise
    except Exception as e:
        # CacheStore 不可用时使用 None(测试场景)— verify_and_consume_capability
        # 会因 nonce_store.consume 抛异常而失败(fail-closed)
        logger.warning(
            f"[backup_dr_validate] R76 O8: nonce_store 初始化失败,"
            f"restore 将 fail-closed: {e}"
        )
        nonce_store = None

    # ── R61 P0-03: 调用私有写入器(R69 Wave 2: 从 services.restore_writer 导入) ──
    from services.restore_writer import _restore_from_backup_data
    result = await _restore_from_backup_data(
        verified_payload,
        capability=capability,
        operation_context=operation_context,
        nonce_store=nonce_store,
        tables=tables,
        merge=merge,
    )
    return result


# ── R62 P0-01: 内部辅助 — 为已通过等效验证的备份发放 capability ───


async def _restore_preverified_payload(
    *,
    data: dict,
    backup_id: str,
    manifest_sha256: str,
    payload_key: str,
    ciphertext_sha256: str,
    plaintext_sha256: str,
    encryption_key_id: str,
    schema_fingerprint: str,
    issuer: str,
    tables: "list[str] | None" = None,
    merge: bool = False,
    created_at: str = "",
) -> dict:
    """R62 P0-01: 为已通过等效验证的备份(如 BackupEngine._restore_internal
    自有的 manifest/ciphertext_sha/decrypt/plaintext_sha 验证路径)发放
    _RestoreCapability 并写入数据库。

    **仅供内部代码使用** — 调用方必须已通过等效的严格验证(下载 manifest →
    校验 ciphertext_sha → 解密 → 校验 plaintext_sha)。本函数为模块私有
    (下划线前缀),与 validate_and_restore_backup_strict 共用 _RESTORE_SENTINEL
    签发 capability,但跳过完整三段式验证(避免重复下载/解密)。

    安全保证:
        - 本函数构造不可伪造的 _RestoreCapability(由 _RESTORE_SENTINEL 保护),
          外部代码无法直接构造令牌调用 _restore_from_backup_data。
        - capability.payload_digest 与 VerifiedBackupPayload.payload_digest 绑定,
          _restore_from_backup_data 首条 assert_valid() 校验二者一致性,
          防止 data 在传递过程中被替换。
        - assert_valid 还校验 schema_fingerprint 一致性(防 scope 跨越)
          与 nonce 防重放(防同一 capability 二次写入)。

    Args:
        data: 已通过等效验证的备份数据 dict(含 "tables" 键)
        backup_id: 备份 ID(来自 manifest.backup_id)
        manifest_sha256: manifest 原始 bytes 的 SHA-256(由调用方计算)
        payload_key: payload.enc 的 R2 key
        ciphertext_sha256: 密文的 SHA-256(来自 manifest.ciphertext_sha256)
        plaintext_sha256: 明文的 SHA-256(来自 manifest.plaintext_sha256)
        encryption_key_id: 加密密钥 ID(来自 manifest.encryption.key_id)
        schema_fingerprint: schema 指纹(通常为 manifest.schema_version,用于 scope 校验)
        issuer: 签发者标识(如 "BackupEngine._restore_internal",用于审计)
        tables: 仅恢复指定表;None 则恢复备份中的所有表
        merge: True=增量补充;False=覆盖(默认)

    Returns:
        dict: _restore_from_backup_data 的结果

    Raises:
        AppError: capability 校验失败(过期/重放/digest 不匹配/scope 跨越)时
                  由 _restore_from_backup_data 首条 assert_valid 抛出
    """
    # R76 P0-05: env 校验必须在 capability 签发前执行 — 缺失即 fail-closed
    _restore_signing_key, _ctx_source_sha, _ctx_run_id, _ctx_run_attempt = (
        _validate_r76_p0_05_env()
    )

    # R62 P0-02 / R64 P1-01: 构造 VerifiedBackupPayload(单一 canonical bytes 来源)
    # payload_digest 从 canonical_payload_bytes 自动计算(__post_init__)
    # R65 P1-06: 用 _enrich_payload_data 补齐 canonical payload 必填字段
    # (version/backup_id/created_at/tables),使构造时强校验通过
    enriched_data = _enrich_payload_data(
        data, backup_id=backup_id, created_at=created_at,
    )
    verified_payload = VerifiedBackupPayload(
        backup_id=backup_id,
        manifest_sha256=manifest_sha256,
        plaintext_sha256=plaintext_sha256,
        schema_fingerprint=schema_fingerprint,
        canonical_payload_bytes=_canonical_json_bytes(enriched_data),
    )

    # R75 P0-06: 签发 capability dict(替代旧 _RestoreCapability 对象)
    from services.restore_capability_file import issue_capability
    # R76 P0-05: _restore_signing_key / _ctx_source_sha / _ctx_run_id /
    # _ctx_run_attempt 已在函数开头通过 _validate_r76_p0_05_env() 校验获取
    _ctx_audience = issuer
    _ctx_target_uri = f"sqlite://{payload_key}"
    # R76 P0-05: operation_id 由 orchestrator 生成;此处使用 backup_id+timestamp
    # 作为确定性 operation_id(backup_dr_validate 路径无 orchestrator)
    _ctx_operation_id = f"restore-{backup_id}-{payload_key}"
    _ctx_nonce = _secrets.token_hex(16)
    capability = issue_capability(
        backup_id=backup_id,
        source_sha=_ctx_source_sha,
        target_database_identity=schema_fingerprint,
        target_path=payload_key,
        run_id=_ctx_run_id,
        run_attempt=_ctx_run_attempt,
        audience=_ctx_audience,
        target_uri=_ctx_target_uri,
        signing_key=_restore_signing_key,
        operation_id=_ctx_operation_id,
        nonce=_ctx_nonce,
    )

    # R76 P0-05 / O8: 构造 RestoreOperationContext(独立 expected 值来源)
    # 与 validate_and_restore_backup_strict 镜像,context 字段从 env + 已验证
    # manifest 字段读取,不依赖 capability 自身回填
    from services.restore_nonce_store import RestoreNonceStore
    from services.restore_operation_context import RestoreOperationContext
    operation_context = RestoreOperationContext(
        operation_id=_ctx_operation_id,
        backup_id=backup_id,
        source_sha=_ctx_source_sha,
        run_id=_ctx_run_id,
        run_attempt=_ctx_run_attempt,
        audience=_ctx_audience,
        target_identity=schema_fingerprint,
        target_uri=_ctx_target_uri,
        manifest_digest=manifest_sha256,
        payload_digest=verified_payload.payload_digest,
        allowed_action="restore_to_blank_target",
        nonce=_ctx_nonce,
    )
    operation_context.validate()  # fail-closed

    # R76 P0-06 / O8: 获取 RestoreNonceStore(数据库 CAS,替代 /tmp 文件 CAS)
    # 当 get_cache_store() 返回 None(单元测试场景)时,跳过 nonce 预留,
    # nonce_store=None 传给 writer,writer 的 verify_and_consume_capability
    # 会在 None.consume 上抛异常(fail-closed),由集成测试覆盖真实 DB 路径
    try:
        from database.cache_store import get_cache_store
        _cache_store = get_cache_store()
        if _cache_store is None:
            logger.debug(
                "[backup_dr_validate] R76 O8: cache_store 不可用 "
                "(_restore_preverified_payload),跳过 nonce 预留(测试场景)"
            )
            nonce_store = None
        else:
            nonce_store = RestoreNonceStore(_cache_store)
            # 预留 nonce(数据库 CAS)
            reserved = await nonce_store.reserve(
                capability, operation_context,
                reserved_by=f"backup_dr_validate:{_ctx_operation_id}",
            )
            if not reserved:
                raise AppError(
                    ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                    params={
                        "reason": "nonce_reserve_failed_replay_detected",
                        "operation_id": _ctx_operation_id,
                    },
                )
    except AppError:
        raise
    except Exception as e:
        # CacheStore 不可用时使用 None(测试场景)— verify_and_consume_capability
        # 会因 nonce_store.consume 抛异常而失败(fail-closed)
        logger.warning(
            f"[backup_dr_validate] R76 O8: nonce_store 初始化失败 "
            f"(_restore_preverified_payload),restore 将 fail-closed: {e}"
        )
        nonce_store = None

    # 调用私有写入器(R69 Wave 2: 从 services.restore_writer 延迟导入)
    from services.restore_writer import _restore_from_backup_data
    return await _restore_from_backup_data(
        verified_payload,
        capability=capability,
        operation_context=operation_context,
        nonce_store=nonce_store,
        tables=tables,
        merge=merge,
    )
