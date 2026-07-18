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

import copy as _copy
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets as _secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

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

        # 6. 原子消费 nonce — INSERT OR IGNORE CAS(R63 P1-01)
        # 多实例/并发安全:rowcount==1 表示本调用方赢得竞态;
        # rowcount==0 表示 nonce 已被消费(重放攻击或竞态失败)。
        # consumed_by: hostname:pid 用于审计(追踪哪个 worker 消费了 nonce)。
        _consumed_by = ""
        try:
            import socket as _socket
            _consumed_by = f"{_socket.gethostname()}:{os.getpid()}"
        except Exception:
            _consumed_by = f"pid:{os.getpid()}"

        if _store is not None:
            try:
                _won = await _store.consume_capability_nonce(
                    self._nonce,
                    self._backup_id,
                    self._manifest_sha256,
                    self._payload_digest,
                    consumed_by=_consumed_by,
                )
            except Exception:
                _won = False
            if not _won:
                # 消费失败 — nonce 已被其他调用方消费(重放或竞态失败)
                raise AppError(
                    ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
                    params={"reason": "nonce_already_consumed"},
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


@dataclass(frozen=True)
class VerifiedBackupPayload:
    """R62 P0-02 / R63 P0-02: 已通过严格验证的备份 payload(深冻结不可变数据载体)。

    由 validate_and_restore_backup_strict() 或 _restore_preverified_payload()
    在严格三段式验证通过后构造。作为 _restore_from_backup_data() 的输入,
    替代原 raw data: dict 参数。

    安全保证(R63 P0-02 增强):
        - frozen=True:顶层字段不可修改(防止在验证后、写入前被篡改)
        - **深冻结(R63 P0-02)**: ``tables`` 与 ``payload`` 在 ``__post_init__``
          中通过 ``_deep_freeze`` 递归转换为 ``MappingProxyType`` + ``tuple``
          不可变结构,且深拷贝断绝与调用方原 dict 的别名引用。嵌套 dict/list
          无法被修改,顶层 object.__setattr__ 由 writer 端重算 digest 兜底。
        - payload_digest 自动由 __post_init__ 从冻结后的 payload 计算(canonical
          JSON sha256),并与 _RestoreCapability.payload_digest 绑定。
        - **writer 端重算(R63 P0-02)**: _restore_from_backup_data 首条语句对
          ``verified_payload.payload`` 实际 canonical bytes 重新计算 SHA-256,
          与 capability.payload_digest 比对。即使 object.__setattr__ 绕过冻结
          替换了 payload,重算 digest 也会与 capability 内嵌(构造时)的 digest
          不匹配 → fail-closed。

    字段:
        backup_id:          备份 ID(来自 manifest.backup_id)
        tables:             表数据(深冻结 MappingProxyType,已解密 + 校验通过)
        manifest_sha256:    manifest 原始 bytes 的 SHA-256(来自 COMPLETE marker 验签)
        plaintext_sha256:   解密后明文的 SHA-256(来自 manifest.plaintext_sha256)
        schema_fingerprint: schema 指纹(通常为 manifest.schema_version,用于 scope 校验)
        payload:            原始 payload(深冻结 MappingProxyType;新代码应从 tables 读取)
        payload_digest:     payload 的 SHA-256 digest(canonical JSON),
                            由 __post_init__ 从冻结后的 payload 自动计算(不可由调用方设置)
    """
    backup_id: str
    tables: dict
    manifest_sha256: str
    plaintext_sha256: str
    schema_fingerprint: str
    payload: dict
    payload_digest: str = ""

    def __post_init__(self):
        # R63 P0-02: 深冻结 tables 与 payload(深拷贝 + MappingProxyType/tuple)
        # 断绝与调用方原 dict 的别名引用,递归冻结嵌套结构。
        # frozen=True 阻止常规赋值,需用 object.__setattr__ 绕过冻结保护。
        object.__setattr__(self, "tables", _deep_freeze(self.tables))
        object.__setattr__(self, "payload", _deep_freeze(self.payload))
        # R62 P0-02: 自动从冻结后的 payload 计算 payload_digest
        # (若调用方未提供,或即使提供了也从冻结后的实际 bytes 重算,保证一致性)
        if not self.payload_digest:
            object.__setattr__(
                self, "payload_digest", _compute_payload_digest(self.payload),
            )


def _compute_payload_digest(data) -> str:
    """R62 P0-02 / R63 P0-02: 计算备份数据的 SHA-256 digest(canonical JSON 序列化)。

    使用 sort_keys=True + separators=(",", ":") + ensure_ascii=False,
    保证相同内容不同 key 顺序产生相同 digest(canonical 形式)。

    R63 P0-02: 支持深冻结结构(MappingProxyType / tuple)—— 先通过
    ``_to_serializable`` 还原为普通 dict/list 再序列化,保证 digest
    计算与普通 dict 一致。

    此 digest 与 _RestoreCapability.payload_digest 绑定,
    在 _restore_from_backup_data 的首条 assert_valid() 调用中由 writer
    重新计算实际 bytes 的 SHA-256 并与 capability 内嵌 digest 比对,
    防止 payload 在验证后、写入前被替换(含 object.__setattr__ 攻击)。

    Args:
        data: 备份数据(普通 dict 或深冻结 MappingProxyType,通常含 "tables" 键)

    Returns:
        64 字符 hex sha256 digest
    """
    # R63 P0-02: 深冻结结构需还原为普通 dict/list 才能 JSON 序列化
    serializable = _to_serializable(data) if isinstance(data, (MappingProxyType, tuple)) else data
    canonical = json.dumps(
        serializable, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
    # R62 P0-01: 信任链元数据(由严格三段式验证填充)
    cap_backup_id = ""
    cap_manifest_sha256 = ""
    cap_payload_key = ""
    cap_ciphertext_sha256 = ""
    cap_plaintext_sha256 = ""
    cap_encryption_key_id = ""
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

    # ── R62 P0-02 / R63 P0-02: 构造 VerifiedBackupPayload(深冻结,不可篡改) ──
    verified_payload = VerifiedBackupPayload(
        backup_id=cap_backup_id,
        tables=data.get("tables", {}),
        manifest_sha256=cap_manifest_sha256,
        plaintext_sha256=cap_plaintext_sha256,
        schema_fingerprint=cap_schema_fingerprint,
        payload=data,
    )

    # ── R61 P0-03 / R62 P0-01: 构造不可伪造的 _RestoreCapability ──
    # 仅本模块可通过 _RESTORE_SENTINEL 构造;外部代码无法获取 sentinel 引用。
    # capability.payload_digest 与 verified_payload.payload_digest 绑定,
    # _restore_from_backup_data 首条语句 assert_valid() 校验二者一致性。
    capability = _RestoreCapability(
        _RESTORE_SENTINEL,
        backup_id=cap_backup_id,
        manifest_sha256=cap_manifest_sha256,
        payload_key=cap_payload_key,
        ciphertext_sha256=cap_ciphertext_sha256,
        plaintext_sha256=cap_plaintext_sha256,
        encryption_key_id=cap_encryption_key_id,
        issuer="validate_and_restore_backup_strict",
        schema_fingerprint=cap_schema_fingerprint,
        payload_digest=verified_payload.payload_digest,
    )

    # ── R61 P0-03: 调用私有写入器(延迟导入避免循环依赖) ──
    # db_restore.py 在 run_restore() 中导入本模块,故此处必须延迟导入。
    from services.db_restore import _restore_from_backup_data
    result = await _restore_from_backup_data(
        verified_payload,
        _capability=capability,
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
    # R62 P0-02: 构造 VerifiedBackupPayload(自动计算 payload_digest)
    verified_payload = VerifiedBackupPayload(
        backup_id=backup_id,
        tables=data.get("tables", {}),
        manifest_sha256=manifest_sha256,
        plaintext_sha256=plaintext_sha256,
        schema_fingerprint=schema_fingerprint,
        payload=data,
    )

    # R62 P0-01: 构造不可伪造的 _RestoreCapability
    # payload_digest 与 verified_payload.payload_digest 绑定,
    # _restore_from_backup_data 首条 assert_valid 校验二者一致(防 payload 替换)。
    capability = _RestoreCapability(
        _RESTORE_SENTINEL,
        backup_id=backup_id,
        manifest_sha256=manifest_sha256,
        payload_key=payload_key,
        ciphertext_sha256=ciphertext_sha256,
        plaintext_sha256=plaintext_sha256,
        encryption_key_id=encryption_key_id,
        issuer=issuer,
        schema_fingerprint=schema_fingerprint,
        payload_digest=verified_payload.payload_digest,
    )

    # 调用私有写入器(延迟导入避免循环依赖)
    from services.db_restore import _restore_from_backup_data
    return await _restore_from_backup_data(
        verified_payload,
        _capability=capability,
        tables=tables,
        merge=merge,
    )
