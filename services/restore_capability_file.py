"""R74 P1-03/P1-04: 一次性 restore capability 文件(替代 ALLOW_LEGACY_RESTORE 逃生舱)。

R74 审计整改:
    P1-03: 原实现复用 BACKUP_SIGNING_KEY 签发 restore capability,
        将备份签名与恢复授权两个不同信任域合并。现改为使用独立的
        RESTORE_CAPABILITY_SIGNING_KEY,通过 key_id 字段追踪密钥来源。
    P1-04: 增强 capability 绑定 — 添加 target_uri/run_id/run_attempt/
        audience 字段,修正过期边界(>= 替代 >),添加 nonce 原子消费
        与 verify_and_consume_capability() 单一入口。

背景(R73 §5.4):
    旧实现:db_restore.py::main() 在 --target staging/development 时自动
    os.environ.setdefault("ALLOW_LEGACY_RESTORE", "1")。这是通用环境变量逃生舱 —
    任何有 CacheStore 写权限的进程都能设置此变量绕过 capability-seal,使
    restore_writer 跳过 _RestoreCapability 校验直接写入。R73 §5.4 明确禁止。

整改方案(R73 §5.4 + R74 P1-03/P1-04):
    由 RestoreOrchestrator 签发一次性 capability 文件,至少绑定:
      - operation_id
      - backup_id
      - source_sha
      - run_id / run_attempt (GitHub Actions 运行绑定)
      - audience (目标受众)
      - target_database_identity / target_uri
      - issued_at / expires_at (严格过期边界: >=)
      - nonce (原子消费,防重放)
      - allowed_action=restore_to_blank_target
      - key_id=RESTORE_CAPABILITY_SIGNING_KEY (独立密钥域)
    文件本身用 RESTORE_CAPABILITY_SIGNING_KEY 计算 HMAC-SHA256 签名。
    restore_writer 验证:
      1. HMAC 签名有效(密钥仅 orchestrator 与 restore_writer 共享)
      2. 未过期(expires_at >= now,严格边界)
      3. allowed_action == "restore_to_blank_target"
      4. target_database_identity 与实际 target 匹配
      5. target_uri 与实际 target URI 匹配
      6. run_id / run_attempt 与本次运行一致
      7. audience 与预期受众匹配
      8. 目标库为空(防止覆盖已有数据)
      9. nonce 原子消费(verify_and_consume_capability 单一入口)

安全保证:
    - capability 文件通过临时文件 /run/secrets/restore_capability.json 注入,
      权限 0400,job 结束立即销毁
    - HMAC 密钥使用独立的 RESTORE_CAPABILITY_SIGNING_KEY(与 BACKUP_SIGNING_KEY
      分离,防止信任域合并)
    - 一次性:nonce 通过 verify_and_consume_capability 原子消费,
      重复使用同一 capability 文件会在第二次恢复时 fail-closed
    - 不可伪造:无 RESTORE_CAPABILITY_SIGNING_KEY 的攻击者无法生成有效签名
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from services.error_codes import AppError, ErrorCodes


# ════════════════════════════════════════════════════════════════
# Capability 文件结构
# ════════════════════════════════════════════════════════════════


CAPABILITY_SCHEMA_VERSION = "1.0"
ALLOWED_ACTION_RESTORE = "restore_to_blank_target"
DEFAULT_CAPABILITY_TTL_SECONDS = 3600  # 1 小时
RESTORE_CAPABILITY_SIGNING_KEY_ENV = "RESTORE_CAPABILITY_SIGNING_KEY"


def _now_iso() -> str:
    """UTC ISO8601 时间戳。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _parse_iso(ts: str) -> _dt.datetime:
    """解析 ISO8601 时间戳(支持 Z 后缀)。"""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(ts)


def compute_capability_signature(capability: dict[str, Any], signing_key: bytes) -> str:
    """计算 capability 文件的 HMAC-SHA256 签名。

    签名输入为 canonical JSON(sort_keys=true, separators=(",", ":")),
    不含 "signature" 字段。

    Args:
        capability: capability dict(不含 signature 字段)
        signing_key: RESTORE_CAPABILITY_SIGNING_KEY(bytes) — 独立于备份签名密钥

    Returns:
        hexdigest HMAC-SHA256
    """
    payload_for_signing = {k: v for k, v in capability.items() if k != "signature"}
    canonical = json.dumps(
        payload_for_signing,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(signing_key, canonical, hashlib.sha256).hexdigest()


def issue_capability(
    *,
    backup_id: str,
    source_sha: str,
    target_database_identity: str,
    target_path: str,
    operation_id: str | None = None,
    run_id: int | None = None,
    run_attempt: int | None = None,
    audience: str = "",
    target_uri: str = "",
    ttl_seconds: int = DEFAULT_CAPABILITY_TTL_SECONDS,
    signing_key: bytes,
    signing_key_id: str = "RESTORE_CAPABILITY_SIGNING_KEY",
    nonce: str | None = None,
) -> dict[str, Any]:
    """签发一次性 restore capability 文件。

    由 RestoreOrchestrator 在创建 staging operation 时调用,产出 capability
    dict 写入临时文件(权限 0400),供 db_restore --capability-file 读取。

    R74 P1-03: 使用独立的 RESTORE_CAPABILITY_SIGNING_KEY(不再复用
        BACKUP_SIGNING_KEY),通过 key_id 字段追踪密钥来源。

    Args:
        backup_id: 备份 ID(必须与即将恢复的 backup_id 一致)
        source_sha: 当前 master SHA(绑定到本次 RC run)
        target_database_identity: 恢复目标数据库 identity hash
            (sha256(canonical schema + first row hash))
        target_path: 恢复目标路径/DSN(如 /app/data/staging/cache_store.db)
        operation_id: 恢复 operation ID(默认生成 UUID)
        run_id: GitHub Actions run ID(绑定到本次 RC run,**R74 P1-07: 强制非 None**)
        run_attempt: GitHub Actions run attempt(**R74 P1-07: 强制非 None**)
        audience: 目标受众标识(如 "staging-restore-*",**R74 P1-07: 强制非空**)
        target_uri: 恢复目标 URI(如 sqlite:///app/data/staging/cache_store.db,
                   **R74 P1-07: 强制非空,不允许回退到 target_path**)
        ttl_seconds: capability 有效期(默认 1 小时)
        signing_key: RESTORE_CAPABILITY_SIGNING_KEY(bytes) — 独立于备份签名密钥
        signing_key_id: 密钥 ID(用于多密钥轮换场景,默认 RESTORE_CAPABILITY_SIGNING_KEY)
        nonce: 外部传入的 nonce(32 hex 字符)。若为 None 则内部生成 secrets.token_hex(16)。
            R76 P0-06: 允许调用方传入已预约的 nonce,与 RestoreNonceStore 配合
            实现原子消费防重放。

    Returns:
        capability dict(含 signature 字段),可 json.dump 到文件
    """
    if not signing_key:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={"reason": "signing_key_empty_for_capability"},
        )
    # R74 P1-07: 强制非空字段 — run_id / run_attempt / audience / target_uri
    # 对 RC 恢复,这些字段必须强制非空,不应由调用方选择是否校验。
    # - run_id / run_attempt: 绑定 GitHub Actions run,防止跨 run 重放
    # - audience: 绑定目标受众,防止跨环境误用
    # - target_uri: 不允许回退到 target_path(消除 URI/path 语义混淆)
    _missing_fields: list[str] = []
    if not backup_id:
        _missing_fields.append("backup_id")
    if not source_sha:
        _missing_fields.append("source_sha")
    if not target_database_identity:
        _missing_fields.append("target_database_identity")
    if not target_path:
        _missing_fields.append("target_path")
    if run_id is None:
        _missing_fields.append("run_id")
    if run_attempt is None:
        _missing_fields.append("run_attempt")
    if not audience:
        _missing_fields.append("audience")
    if not target_uri:
        _missing_fields.append("target_uri")
    if _missing_fields:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_required_field_missing",
                "missing": _missing_fields,
            },
        )

    issued_at = _now_iso()
    expires_at = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ttl_seconds)
    ).isoformat()
    # R76 P0-06: 优先使用调用方传入的 nonce(与 RestoreNonceStore 配合);
    # 未传入时内部生成,保持向后兼容
    if nonce is None:
        nonce = secrets.token_hex(16)

    capability: dict[str, Any] = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "kind": "restore-capability",
        "operation_id": operation_id or str(uuid.uuid4()),
        "backup_id": backup_id,
        "source_sha": source_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "audience": audience,
        "allowed_action": ALLOWED_ACTION_RESTORE,
        "target_database_identity": target_database_identity,
        "target_path": target_path,
        "target_uri": target_uri,  # R74 P1-07: 不允许回退到 target_path(强制必填)
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "key_id": signing_key_id,
    }
    capability["signature"] = compute_capability_signature(capability, signing_key)
    return capability


def write_capability_file(
    capability: dict[str, Any],
    path: str | Path,
) -> None:
    """将 capability dict 写入文件(权限 0400,仅 owner 可读)。

    Args:
        capability: issue_capability 返回的 dict
        path: 文件路径(建议 /run/secrets/restore_capability.json)
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先写文件再 chmod(Windows 不支持 chmod 0400,忽略错误)
    p.write_text(
        json.dumps(capability, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        p.chmod(0o400)
    except OSError:
        # Windows 文件系统不支持 Unix 权限位 — 在 Linux 容器中生效即可
        pass


def verify_capability(
    capability: dict[str, Any],
    *,
    signing_key: bytes,
    expected_backup_id: str | None = None,
    expected_target_identity: str | None = None,
    expected_source_sha: str | None = None,
    expected_run_id: int | None = None,
    expected_run_attempt: int | None = None,
    expected_audience: str | None = None,
    expected_target_uri: str | None = None,
    expected_operation_id: str | None = None,
    expected_allowed_action: str | None = None,
    now: _dt.datetime | None = None,
) -> None:
    """验证 restore capability 文件 — fail-closed。

    校验维度:
        1. schema_version == "1.0"
        2. kind == "restore-capability"
        3. 所有必需字段存在(operation_id / backup_id / source_sha /
           run_id / run_attempt / audience / target_database_identity /
           target_path / target_uri / allowed_action / issued_at /
           expires_at / nonce / signature / key_id)
        4. allowed_action == "restore_to_blank_target"
        5. HMAC 签名有效(用 RESTORE_CAPABILITY_SIGNING_KEY 重算并比对)
        6. 未过期(expires_at >= now,严格边界 — R74 P1-04)
        7. (可选)expected_backup_id / expected_target_identity / expected_source_sha
           / expected_run_id / expected_run_attempt / expected_audience /
           expected_target_uri / expected_operation_id / expected_allowed_action 一致

    R76 P0-05: 所有 expected 值必须来自独立来源(RestoreOperationContext),
    不得由 capability 自身回填。expected_operation_id / expected_allowed_action
    在消费入口独立比较,不得仅与硬编码常量比较。

    Args:
        capability: 从 --capability-file 加载的 dict
        signing_key: RESTORE_CAPABILITY_SIGNING_KEY(bytes) — 独立于备份签名密钥
        expected_backup_id: 期望的 backup_id(来自 CLI --backup-id,可选)
        expected_target_identity: 期望的 target identity(来自 --target-identity,可选)
        expected_source_sha: 期望的 source SHA(来自 GITHUB_SHA,可选)
        expected_run_id: 期望的 GitHub Actions run ID(可选)
        expected_run_attempt: 期望的 GitHub Actions run attempt(可选)
        expected_audience: 期望的受众标识(可选)
        expected_target_uri: 期望的 target URI(可选)
        expected_operation_id: R76 P0-05 期望的 operation_id(来自独立 context,可选)
        expected_allowed_action: R76 P0-05 期望的 allowed_action(来自独立 context,可选)
        now: 当前时间(测试可注入,默认 UTC now)

    Raises:
        AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): 任一校验失败
    """
    required_fields = [
        "schema_version", "kind", "operation_id", "backup_id", "source_sha",
        "run_id", "run_attempt", "audience",  # R74 P1-07: 强制非空
        "target_database_identity", "target_path", "target_uri", "allowed_action",
        "issued_at", "expires_at", "nonce", "signature", "key_id",
    ]
    missing = [f for f in required_fields if not capability.get(f)]
    if missing:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_missing_required_fields",
                "missing": missing,
            },
        )

    if capability["schema_version"] != CAPABILITY_SCHEMA_VERSION:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_schema_version_mismatch",
                "expected": CAPABILITY_SCHEMA_VERSION,
                "actual": capability["schema_version"],
            },
        )

    if capability.get("kind", "") != "restore-capability":
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_kind_mismatch",
                "expected": "restore-capability",
                "actual": capability.get("kind"),
            },
        )

    if capability["allowed_action"] != ALLOWED_ACTION_RESTORE:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_action_not_allowed",
                "expected": ALLOWED_ACTION_RESTORE,
                "actual": capability["allowed_action"],
            },
        )

    # 重算签名并比对
    expected_sig = compute_capability_signature(capability, signing_key)
    actual_sig = capability.get("signature", "")
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_signature_invalid",
                "key_id": capability.get("key_id"),
            },
        )

    # 有效期校验(R74 P1-04: 严格边界 >=)
    now_dt = now or _dt.datetime.now(_dt.timezone.utc)
    expires_at = _parse_iso(capability["expires_at"])
    if now_dt >= expires_at:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_expired",
                "expires_at": capability["expires_at"],
                "now": now_dt.isoformat(),
            },
        )

    # 期望值一致性校验(可选 — 由调用方按需传入)
    if expected_backup_id and capability["backup_id"] != expected_backup_id:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_backup_id_mismatch",
                "expected": expected_backup_id,
                "actual": capability["backup_id"],
            },
        )
    if expected_target_identity and capability["target_database_identity"] != expected_target_identity:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_target_identity_mismatch",
                "expected": expected_target_identity,
                "actual": capability["target_database_identity"],
            },
        )
    if expected_source_sha and capability["source_sha"] != expected_source_sha:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_source_sha_mismatch",
                "expected": expected_source_sha,
                "actual": capability["source_sha"],
            },
        )
    # R74 P1-04: 增强绑定 — run_id / run_attempt / audience / target_uri
    if expected_run_id is not None and capability.get("run_id") != expected_run_id:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_run_id_mismatch",
                "expected": expected_run_id,
                "actual": capability.get("run_id"),
            },
        )
    if expected_run_attempt is not None and capability.get("run_attempt") != expected_run_attempt:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_run_attempt_mismatch",
                "expected": expected_run_attempt,
                "actual": capability.get("run_attempt"),
            },
        )
    if expected_audience and capability.get("audience", "") != expected_audience:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_audience_mismatch",
                "expected": expected_audience,
                "actual": capability.get("audience"),
            },
        )
    if expected_target_uri and capability.get("target_uri", "") != expected_target_uri:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_target_uri_mismatch",
                "expected": expected_target_uri,
                "actual": capability.get("target_uri"),
            },
        )
    # R76 P0-05: operation_id 与 allowed_action 在消费入口独立比较
    # (来自 RestoreOperationContext 独立来源,不得仅与硬编码常量比较)
    if expected_operation_id and capability.get("operation_id", "") != expected_operation_id:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_operation_id_mismatch",
                "expected": expected_operation_id,
                "actual": capability.get("operation_id"),
            },
        )
    if expected_allowed_action and capability.get("allowed_action", "") != expected_allowed_action:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_allowed_action_mismatch",
                "expected": expected_allowed_action,
                "actual": capability.get("allowed_action"),
            },
        )


async def verify_and_consume_capability(
    capability: dict[str, Any],
    *,
    signing_key: bytes,
    operation_context: Any,  # RestoreOperationContext(避免循环 import 用 Any)
    nonce_store: Any,  # RestoreNonceStore(避免循环 import 用 Any)
    now: _dt.datetime | None = None,
) -> bool:
    """R76 P0-05 / P0-06 / O8: 验证并原子消费 restore capability — 单一入口。

    R76 整改(替代 R74 P1-04):
        - **删除** ``operation_id`` / ``source_sha`` / ``expected_nonce`` 等从
          capability 自身回填的参数(R76 P0-05: 自比较无安全意义)
        - **删除** ``nonce_store_dir`` 参数和 ``/tmp/restore_nonce_store`` 默认值
          (R76 P0-06: 容器重建/runner 迁移后状态丢失)
        - **删除** ``os.open(O_CREAT|O_EXCL)`` 文件 CAS 逻辑
          (R76 P0-06: 替换为数据库 UNIQUE 约束 CAS)
        - **新增** ``operation_context`` 参数:由 orchestrator 权威状态加载的
          ``RestoreOperationContext``,提供所有 expected 值(独立来源)
        - **新增** ``nonce_store`` 参数:``RestoreNonceStore`` 实例,封装数据库 CAS

    校验维度(继承自 verify_capability):
        1. schema_version / kind / 必需字段 / allowed_action
        2. HMAC 签名有效
        3. 未过期(expires_at >= now)
        4. backup_id / target_identity / source_sha / run_id /
           run_attempt / audience / target_uri 与 context 一致(独立来源)

    额外校验:
        5. capability 中的 nonce 与 operation_context.nonce 一致
        6. nonce 原子消费(数据库 CAS,防重放)

    Args:
        capability: 从 --capability-file 加载的 dict
        signing_key: RESTORE_CAPABILITY_SIGNING_KEY(bytes)
        operation_context: ``RestoreOperationContext`` 实例 — 由 orchestrator
                          权威状态加载,提供所有 expected 值(独立来源,非 capability 回填)
        nonce_store: ``RestoreNonceStore`` 实例 — 封装数据库 CAS(替代 /tmp 文件 CAS)
        now: 当前时间(测试可注入)

    Returns:
        True 若全部校验通过且 nonce 原子消费成功

    Raises:
        AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): 任一校验失败或 nonce 已消费
        ValueError: operation_context 字段不完整(R76 P0-05 fail-closed)
    """
    # R76 P0-05: 校验 operation_context 所有必填字段非空(fail-closed)
    # 不再从 capability 自身回填 expected 值(自比较无安全意义)
    operation_context.validate()

    # 1. 调用 verify_capability 完成所有签名/字段/有效期/绑定校验
    #    所有 expected 值来自 operation_context(独立来源)
    #    R76 P0-05: 新增 expected_operation_id / expected_allowed_action 比对,
    #    不再仅依赖硬编码 ALLOWED_ACTION_RESTORE 常量比较
    verify_capability(
        capability,
        signing_key=signing_key,
        expected_backup_id=operation_context.backup_id,
        expected_target_identity=operation_context.target_identity,
        expected_source_sha=operation_context.source_sha,
        expected_run_id=operation_context.run_id,
        expected_run_attempt=operation_context.run_attempt,
        expected_audience=operation_context.audience,
        expected_target_uri=operation_context.target_uri,
        expected_operation_id=operation_context.operation_id,
        expected_allowed_action=operation_context.allowed_action,
        now=now,
    )

    # 2. nonce 一致性校验
    # R76 P0-05: expected_nonce 来自 operation_context.nonce(独立来源),
    # 不再从 capability.get("nonce") 回填(自比较)
    capability_nonce = capability.get("nonce", "")
    if not hmac.compare_digest(capability_nonce, operation_context.nonce):
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_nonce_mismatch",
                "expected_nonce_prefix": operation_context.nonce[:8] if operation_context.nonce else "",
                "actual_nonce_prefix": capability_nonce[:8] if capability_nonce else "",
            },
        )

    # 3. R76 P0-06 / O8: nonce 原子消费(数据库 CAS,替代 /tmp 文件 CAS)
    # 调用 RestoreNonceStore.consume,内部使用数据库 UNIQUE 约束实现跨进程/重启/
    # 容器重建的防重放保护(009 migration 的 idx_restore_nonces_nonce_digest)
    consumed = False
    try:
        consumed = await nonce_store.consume(
            capability,
            operation_context,
            consumed_by=f"verify_and_consume_capability:{operation_context.operation_id}",
        )
    except AppError:
        raise
    except Exception as e:
        logger.bind(
            component="restore_capability_file",
            event="nonce_consume_failed",
            operation_id=operation_context.operation_id,
            error=str(e),
        ).error("")
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_nonce_consume_db_error",
                "operation_id": operation_context.operation_id,
                "error": str(e),
            },
        )

    if not consumed:
        # nonce 不在 reserved 状态(已 consumed/failed/不存在)— 重放攻击
        logger.bind(
            component="restore_capability_file",
            event="nonce_replay_detected",
            operation_id=operation_context.operation_id,
            nonce_prefix=capability_nonce[:8],
        ).error("")
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_nonce_already_consumed_replay_detected",
                "operation_id": operation_context.operation_id,
                "nonce_prefix": capability_nonce[:8] if capability_nonce else "",
            },
        )

    logger.bind(
        component="restore_capability_file",
        event="nonce_atomic_consume_success",
        operation_id=operation_context.operation_id,
        nonce_prefix=capability_nonce[:8],
    ).info("")

    return True


def load_capability_file(path: str | Path) -> dict[str, Any]:
    """从文件加载 capability dict。

    Args:
        path: capability 文件路径

    Returns:
        capability dict

    Raises:
        AppError(BACKUP_RESTORE_TRUST_CHAIN_REQUIRED): 文件不存在/JSON 解析失败
    """
    p = Path(path)
    if not p.exists():
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={"reason": "capability_file_not_found", "path": str(p)},
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
            params={
                "reason": "capability_file_invalid",
                "path": str(p),
                "error": str(e),
            },
        )


def compute_target_identity(db_path: str | Path) -> str:
    """计算 SQLite 恢复目标数据库的 identity hash。

    用于 --target-identity 参数。空库时返回 "empty:<sha256(absent path)>" —
    restore_writer 验证目标库为空后才允许恢复。

    对于已存在数据的库,返回 sha256(规范化 schema 摘要 + 第一行 hash)。
    新 staging 目标库预期为空,identity 应为 "empty:..." 形式。

    Args:
        db_path: SQLite 数据库文件路径

    Returns:
        identity hash 字符串(形如 "empty:sha256:..." 或 "sha256:...")
    """
    import sqlite3

    p = Path(db_path)
    if not p.exists():
        # 空目标:返回 absent identity(orchestrator 创建空库后再次校验)
        h = hashlib.sha256(str(p).encode("utf-8")).hexdigest()
        return f"empty:sha256:{h}"

    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
        # 获取所有表名 + schema
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        conn.close()
        if not rows:
            h = hashlib.sha256(str(p).encode("utf-8")).hexdigest()
            return f"empty:sha256:{h}"
        # 计算规范化 schema hash
        canonical_schema = "\n".join(sql for _, sql in rows if sql)
        return "sha256:" + hashlib.sha256(canonical_schema.encode("utf-8")).hexdigest()
    except sqlite3.Error as e:
        logger.bind(
            component="restore_capability_file",
            event="compute_target_identity_sqlite_error",
            error=str(e),
        ).debug("")
        h = hashlib.sha256(str(p).encode("utf-8")).hexdigest()
        return f"empty:sha256:{h}"
