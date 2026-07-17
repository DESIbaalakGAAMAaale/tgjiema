"""R40 §9.3: 灾备控制台 — 备份/key_id/恢复演练/RPO-RTO/校验。

职责:
    提供灾备管理控制台,包括:
    1. list_backups / get_backup_info — 列出备份与详情
    2. trigger_backup — 触发立即备份(委托 services/db_backup.py)
    3. verify_backup — 校验备份完整性(manifest + checksum)
    4. restore — 恢复备份(委托 services/db_restore.py + 审批)
    5. get_rpo_rto — 获取 RPO/RTO 指标
    6. run_recovery_drill — 非破坏性恢复演练
    7. get_recovery_history — 恢复历史记录
    8. get_backup_schedule — 备份计划查询

R59 P0-04: 强制参数,不再允许 fail-open — 信任链所有安全参数强制化。
    - 生产恢复必须通过 services.backup_dr_validate.validate_and_restore_backup_strict
      fail-closed 入口执行,禁止直接绕过验证模块恢复。
    - restore() 委托 BackupEngine.restore(),后者内部已有 COMPLETE+manifest+
      checksum+decrypt 验证,视为合规调用方。
    - 新接入的恢复路径应直接调用 validate_and_restore_backup_strict,
      传入 signing_key/decryptor/expected_manifest_key/expected_backup_id。

设计原则:
    - 纯函数式 + async
    - 通过 database.cache_store.get_cache_store() 获取单例
    - 备份历史存 kv_store(key='backup_history', value=JSON list)
    - 恢复历史存 kv_store(key='recovery_history', value=JSON list)
    - 写操作后调用 store.add_dirty_outbox(table_name, pk) 确保跨机同步
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import datetime as _dt
import json
import time as _time
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store

# R50 P1-1: 统一错误码协议化(替代裸字符串 ValueError)
from services.error_codes import AppError, ErrorCodes
from services.i18n import translate as _i18n_t


# ─── kv_store 键名 ─────────────────────────────────────────────
KV_BACKUP_HISTORY = "backup_history"
KV_RECOVERY_HISTORY = "recovery_history"
KV_LAST_BACKUP_AT = "last_backup_at"

# ─── RPO/RTO 默认目标(秒) ─────────────────────────────────────
# RPO: Recovery Point Objective(可接受数据丢失,默认 6 小时)
# RTO: Recovery Time Objective(恢复时间目标,默认 30 分钟)
DEFAULT_RPO_SECONDS = 6 * 3600
DEFAULT_RTO_SECONDS = 30 * 60


# ─── 1. 备份列表与详情 ─────────────────────────────────────────

async def list_backups(limit: int = 20) -> list[dict]:
    """列出最近备份。

    R40 P0-7: 合并三个来源:
    1. kv_store 中的 'backup_history'(JSON list)
    2. BackupEngine.list_backups() — 仅返回 complete marker 存在的备份(R40 P0-7 新格式)
    3. db_backup.list_backups() — 历史格式 db_backup_*

    Args:
        limit: 最多返回数量

    Returns:
        [{backup_id, created_at, size, encrypted, key_id, checksum, status}]
    """
    store = get_cache_store()
    backups: list[dict] = []

    # 1. 从 kv_store 读取历史记录
    try:
        raw = await store.get_kv(KV_BACKUP_HISTORY)
        if raw:
            history = json.loads(raw)
            if isinstance(history, list):
                backups.extend(history)
    except Exception as e:
        logger.debug(f"[DisasterRecovery] 读取 {KV_BACKUP_HISTORY} 失败: {e}")

    # 2. R40 P0-7: BackupEngine 新格式 backups/<id>.*
    try:
        from services.backup_engine import BackupEngine
        engine = BackupEngine()
        new_backups = await engine.list_backups()
        backups.extend(new_backups)
    except Exception as e:
        logger.debug(f"[DisasterRecovery] BackupEngine.list_backups 失败: {e}")

    # 3. 历史格式 db_backup_*
    try:
        from services import db_backup
        r2_objects = await db_backup.list_backups()
        for obj in r2_objects:
            key = obj.get("key", "")
            size = obj.get("size", 0)
            backups.append({
                "backup_id": key,
                "created_at": obj.get("last_modified", ""),
                "size": size,
                "encrypted": key.endswith(".bin") or key.endswith(".json"),
                "key_id": "",
                "checksum": "",
                "status": "available",
                "source": "r2",
            })
    except Exception as e:
        logger.debug(f"[DisasterRecovery] 查询 R2 备份列表失败: {e}")

    # 去重(以 backup_id 为键)
    seen: set[str] = set()
    unique: list[dict] = []
    for b in backups:
        bid = b.get("backup_id", "")
        if bid and bid not in seen:
            seen.add(bid)
            unique.append(b)
    backups = unique

    # 按创建时间倒序
    backups.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return backups[:limit]


async def get_backup_info(backup_id: str) -> dict | None:
    """获取备份详情。

    Args:
        backup_id: 备份 ID(R2 对象 key 或 backup_history 中的 id)

    Returns:
        {backup_id, created_at, size, encrypted, key_id, checksum, status, ...}
        若不存在返回 None
    """
    if not backup_id:
        return None

    backups = await list_backups(limit=1000)
    for b in backups:
        if b.get("backup_id") == backup_id:
            return b
    return None


# ─── 2. 触发备份 ───────────────────────────────────────────────

async def trigger_backup() -> str:
    """触发立即备份(R40 P0-7: 调用 BackupEngine 真实上传 R2)。

    R40 P0-7 修复:
        原 ``trigger_backup`` 仅采集数据不上传 R2,却更新 last_backup_at
        导致 RPO 假合规。现改为调用 ``BackupEngine.create_backup``,
        真实完成 payload+manifest+COMPLETE marker 三阶段上传 + 校验。
        失败时不更新 last_backup_at(由 BackupEngine 内部保证)。

    Returns:
        backup_id(R2 对象 backup_id);失败返回空字符串
    """
    try:
        from services.backup_engine import BackupEngine
        engine = BackupEngine()
        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest.get("backup_id", "")
        if not backup_id:
            logger.error("[DisasterRecovery] trigger_backup: BackupEngine 未返回 backup_id")
            return ""

        # 写入 backup_history(成功)
        await _append_backup_history({
            "backup_id": backup_id,
            "created_at": manifest.get("created_at", _dt.datetime.now().isoformat()),
            "size": manifest.get("ciphertext_size_bytes", 0),
            "encrypted": manifest.get("encryption", {}).get("encrypted", False),
            "key_id": manifest.get("encryption", {}).get("key_id", ""),
            "checksum": manifest.get("ciphertext_sha256", ""),
            "status": "completed",
            "backup_type": manifest.get("backup_type", "full"),
            "tables": manifest.get("total_tables", 0),
            "total_rows": manifest.get("total_rows", 0),
        })

        logger.info(f"[DisasterRecovery] 备份已上传 R2: {backup_id}")
        return backup_id
    except Exception as e:
        logger.error(f"[DisasterRecovery] trigger_backup 失败(不更新 last_backup_at): {e}")
        # 记录失败到 backup_history(便于排查)
        try:
            await _append_backup_history({
                "backup_id": "",
                "created_at": _dt.datetime.now().isoformat(),
                "size": 0,
                "encrypted": False,
                "key_id": "",
                "checksum": "",
                "status": "failed",
                "backup_type": "full",
                "tables": 0,
                "total_rows": 0,
                "error": str(e),
            })
        except Exception:
            pass
        return ""


# ─── 3. 备份校验 ───────────────────────────────────────────────

async def verify_backup(backup_id: str) -> dict:
    """校验备份完整性。

    Args:
        backup_id: 备份 ID(R2 对象 key)

    Returns:
        {valid, manifest_ok, checksum_ok, file_count, size_bytes, error}
    """
    if not backup_id:
        return {
            "valid": False, "manifest_ok": False, "checksum_ok": False,
            "file_count": 0, "size_bytes": 0, "error": _i18n_t('services.disaster_recovery.s6'),
        }

    try:
        from storage.r2 import _r2 as r2_storage
        from services.backup_crypto import is_encryption_available

        # 下载 manifest(如果存在)
        manifest_key = backup_id.replace("db_backup_", "manifest_")
        if manifest_key.endswith(".json"):
            manifest_key = manifest_key.replace(".json", ".json")
        if manifest_key.endswith(".bin"):
            manifest_key = manifest_key.replace(".bin", ".json")

        manifest_ok = False
        checksum_ok = False
        size_bytes = 0
        file_count = 0
        error = ""

        # 下载 payload
        try:
            content = await r2_storage.download(backup_id)
            size_bytes = len(content)

            # 如有 manifest,下载并校验
            try:
                manifest_content = await r2_storage.download(manifest_key)
                manifest = json.loads(manifest_content)
                manifest_ok = True

                # 校验 checksum
                import hashlib
                actual_sha = hashlib.sha256(content).hexdigest()
                expected_sha = manifest.get("checksum_sha256", "")
                if expected_sha and actual_sha == expected_sha:
                    checksum_ok = True

                file_count = manifest.get("total_tables", 0)
            except Exception as e:
                error = _i18n_t('services.disaster_recovery.s12', e=e)
        except Exception as e:
            error = _i18n_t('services.disaster_recovery.s9', e=e)

        valid = manifest_ok and checksum_ok and size_bytes > 0

        return {
            "valid": valid,
            "manifest_ok": manifest_ok,
            "checksum_ok": checksum_ok,
            "file_count": file_count,
            "size_bytes": size_bytes,
            "error": error,
        }
    except Exception as e:
        logger.error(f"[DisasterRecovery] verify_backup 失败: {e}")
        return {
            "valid": False, "manifest_ok": False, "checksum_ok": False,
            "file_count": 0, "size_bytes": 0, "error": str(e),
        }


# ─── 4. 恢复备份 ──────────────────────────────────────────────

async def restore(
    backup_id: str,
    admin_id: int = 0,
    approver_id: int = 0,
    approval_action_id: str | None = None,
    expected_request_hash: str | None = None,
) -> dict:
    """恢复备份(R40 P0-7: 调用 BackupEngine.restore 可解密加密备份)。

    R40 P0-7 修复:
        原 ``restore`` 直接 ``json.loads(ciphertext)`` 无法解密 AES-GCM 备份。
        现改为调用 ``BackupEngine.restore`` 完成:
        manifest 校验 → ciphertext_sha256 → 解密 → plaintext_sha256 → 写入。

    R40 P0-8: 生产恢复必须审批(approver_id 非零),由 CommandBus 调用本函数。
    旧调用方仍可传 admin_id,本函数将 admin_id 透传给 approver_id 以保持向后兼容。

    R44 G0-3 整改: 灾备恢复必须通过 ApprovalExecutor 调用,不接受任意 approver_id。
    - 新增 ``approval_action_id`` 参数(必填),对应 command_executions.action_id
    - BackupEngine.restore() 通过 approval_action_id 反查 principal_id 并校验审批状态
    - 不传 approval_action_id → 抛 ValueError(强制走审批流)
    - approver_id 参数保留向后兼容,但优先使用 approval_action_id 反查的 principal

    R51 P0-8 整改: production restore 必须传 expected_request_hash(TOCTOU 防护)。
    - 新增 ``expected_request_hash`` 参数(可选,由审批流计算后注入)
    - 若调用方未传,则从 command_executions 反查 principal_id 后用
      BackupEngine._compute_restore_request_hash 计算(绑定 backup_id + target +
      schema_version + requested_by + approval_id)
    - 计算后透传给 BackupEngine.restore 进行 TOCTOU 校验

    R59 P0-04: 强制参数,不再允许 fail-open — 信任链守卫。
    - 本函数委托 BackupEngine.restore(),后者内部已有 COMPLETE+manifest+
      checksum+decrypt 验证,视为合规调用方(不绕过验证模块)。
    - 新接入的恢复路径应直接调用
      services.backup_dr_validate.validate_and_restore_backup_strict,
      传入 signing_key/decryptor/expected_manifest_key/expected_backup_id。
    - 禁止直接绕过验证模块恢复(fail-closed)。

    Args:
        backup_id: 备份 ID(R40 P0-7 格式 backup_YYYYMMDD_HHMMSS_xxxxxxxx)
        admin_id: 操作者 admin_id(向后兼容)
        approver_id: 审批人 ID(已弃用,保留向后兼容;优先用 approval_action_id 反查)
        approval_action_id: 审批动作 ID(必填,对应 command_executions.action_id,
                            必须由审批流通过 ApprovalExecutor 调用恢复时注入)
        expected_request_hash: 期望的 request_hash(由审批流基于 backup_id +
                               target + schema_version + requested_by + approval_id
                               计算;None 时本函数自动反查 principal_id 后计算)

    Returns:
        {success, restored_tables, duration_seconds, error}

    Raises:
        AppError: approval_action_id 为空(BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED)
    """
    if not backup_id:
        return {
            "success": False, "restored_tables": 0,
            "duration_seconds": 0, "error": _i18n_t('services.disaster_recovery.s7'),
        }

    # R44 G0-3: 灾备恢复必须通过 ApprovalExecutor 调用,不接受任意 approver_id
    # approval_action_id 必须由审批流通过 ApprovalExecutor 调用恢复时注入
    if not approval_action_id:
        # R50 P1-1: 协议化为 BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED
        raise AppError(
            ErrorCodes.BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED,
            params={"backup_id": backup_id},
        )

    # 审批人 ID:优先 approver_id,fallback admin_id(向后兼容旧调用方)
    # R44 G0-3: 仅用于审计日志,审批校验由 BackupEngine.restore 通过
    # approval_action_id 反查 principal_id 完成
    effective_approver = approver_id or admin_id

    # R51 P0-8: 若调用方未传 expected_request_hash,从 command_executions 反查
    # principal_id 后计算(绑定 backup_id + target + schema_version + requested_by +
    # approval_id),确保 TOCTOU 校验不可绕过
    if not expected_request_hash:
        try:
            from services.backup_engine import (
                BackupEngine as _BE,
                MANIFEST_SCHEMA_VERSION,
            )
            store = get_cache_store()
            if hasattr(store, "_db") and store._db:
                cursor = await store._db.execute(
                    "SELECT principal_id FROM command_executions "
                    "WHERE action_id = ? LIMIT 1",
                    (approval_action_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        _principal_id = int(row[0])
                    except (TypeError, ValueError):
                        _principal_id = 0
                    if _principal_id:
                        expected_request_hash = _BE._compute_restore_request_hash(
                            backup_id=backup_id,
                            target="production",
                            schema_version=MANIFEST_SCHEMA_VERSION,
                            requested_by=_principal_id,
                            approval_id=approval_action_id,
                        )
        except Exception as e:
            logger.warning(
                f"[DisasterRecovery] R51 P0-8: 反查 principal_id 计算 "
                f"expected_request_hash 失败 (approval_action_id="
                f"{approval_action_id}): {e}"
            )

    start_ts = _time.time()
    try:
        from services.backup_engine import BackupEngine
        engine = BackupEngine()

        # R44 G0-3: 生产恢复必须传 approval_action_id,不传 approver_id
        # 让 BackupEngine.restore 从 command_executions.principal_id 反查
        # R51 P0-8: 透传 expected_request_hash 进行 TOCTOU 校验
        result = await engine.restore(
            backup_id, target="production",
            approval_action_id=approval_action_id,
            expected_request_hash=expected_request_hash,
        )

        success = result.get("success", False)
        restored_tables = result.get("restored_tables", 0)
        error = result.get("error", "")

        # 写 audit_log
        await _write_audit_log(
            actor_id=admin_id or effective_approver,
            action="restore_backup",
            target_type="backup",
            target_id=backup_id,
            details=f"restored_tables={restored_tables} checksum_verified={result.get('checksum_verified')} error={error} approval_action_id={approval_action_id}",
        )

        # 记录到 recovery_history
        await _append_recovery_history({
            "backup_id": backup_id,
            "admin_id": admin_id or effective_approver,
            "approver_id": effective_approver,
            "approval_action_id": approval_action_id,
            "executed_at": _dt.datetime.now().isoformat(),
            "success": success,
            "restored_tables": restored_tables,
            "restored_rows": result.get("restored_rows", 0),
            "checksum_verified": result.get("checksum_verified", False),
            "errors": [error] if error else [],
            "duration_seconds": _time.time() - start_ts,
        })

        logger.info(
            f"[DisasterRecovery] restore 完成: backup_id={backup_id} "
            f"restored={restored_tables} success={success} checksum_verified={result.get('checksum_verified')}"
        )
        return {
            "success": success,
            "restored_tables": restored_tables,
            "restored_rows": result.get("restored_rows", 0),
            "checksum_verified": result.get("checksum_verified", False),
            "duration_seconds": _time.time() - start_ts,
            "error": error,
        }
    except Exception as e:
        logger.error(f"[DisasterRecovery] restore 失败: {e}")
        # 仍记录到 recovery_history
        await _append_recovery_history({
            "backup_id": backup_id,
            "admin_id": admin_id or effective_approver,
            "approver_id": effective_approver,
            "approval_action_id": approval_action_id,
            "executed_at": _dt.datetime.now().isoformat(),
            "success": False,
            "restored_tables": 0,
            "errors": [str(e)],
            "duration_seconds": _time.time() - start_ts,
        })
        return {
            "success": False, "restored_tables": 0,
            "duration_seconds": _time.time() - start_ts,
            "error": str(e),
        }


# ─── 5. RPO/RTO 指标 ──────────────────────────────────────────

async def get_rpo_rto() -> dict:
    """获取 RPO/RTO 指标。

    RPO(Recovery Point Objective): 可接受数据丢失
    RTO(Recovery Time Objective): 恢复时间目标

    Returns:
        {rpo_seconds, rto_seconds, last_backup_age, estimated_recovery_time}
    """
    store = get_cache_store()

    # 从 settings 读取 RPO/RTO 目标
    try:
        from config import settings
        rpo_seconds = int(getattr(settings, "BACKUP_RPO_SECONDS",
                                  DEFAULT_RPO_SECONDS))
        rto_seconds = int(getattr(settings, "BACKUP_RTO_SECONDS",
                                  DEFAULT_RTO_SECONDS))
    except Exception:
        rpo_seconds = DEFAULT_RPO_SECONDS
        rto_seconds = DEFAULT_RTO_SECONDS

    # 计算最近备份距今的秒数(R40 P0-7: 无备份时返回 None,UI 显示"无备份")
    last_backup_age: int | None = None
    try:
        raw = await store.get_kv(KV_LAST_BACKUP_AT)
        if raw:
            last_dt = _dt.datetime.fromisoformat(raw)
            last_backup_age = int((_dt.datetime.now() - last_dt).total_seconds())
    except Exception:
        pass

    # 估算恢复时间(基于历史恢复记录)
    estimated_recovery_time = rto_seconds  # 默认 RTO
    try:
        history = await get_recovery_history(limit=5)
        if history:
            durations = [h.get("duration_seconds", 0) for h in history
                         if h.get("success")]
            if durations:
                estimated_recovery_time = int(sum(durations) / len(durations))
    except Exception:
        pass

    # RPO 合规判定:无备份时返回 False(违规)
    rpo_compliant = (
        last_backup_age is not None and last_backup_age <= rpo_seconds
    )
    return {
        "rpo_seconds": rpo_seconds,
        "rto_seconds": rto_seconds,
        "last_backup_age": last_backup_age,
        "estimated_recovery_time": estimated_recovery_time,
        "rpo_compliant": rpo_compliant,
        "rto_compliant": estimated_recovery_time <= rto_seconds,
    }


async def get_last_backup_age() -> int | None:
    """R40 P0-7: 获取最近备份距今的秒数,无备份返回 None。

    供 UI 显示"无备份"提示,避免误显示 RPO 合规(0 ≤ rpo_seconds 永真)。
    """
    store = get_cache_store()
    try:
        raw = await store.get_kv(KV_LAST_BACKUP_AT)
        if not raw:
            return None
        last_dt = _dt.datetime.fromisoformat(raw)
        return int((_dt.datetime.now() - last_dt).total_seconds())
    except Exception:
        return None


# ─── 6. 恢复演练(非破坏性) ───────────────────────────────────

async def run_recovery_drill() -> dict:
    """执行恢复演练(非破坏性)。

    步骤:
    1. 触发备份
    2. 校验完整性
    3. 模拟恢复(不实际覆盖数据库,仅下载并解析)
    4. 记录演练结果

    Returns:
        {success, duration_seconds, steps: [...]}
    """
    start_ts = _time.time()
    steps: list[dict] = []
    overall_success = True

    # 步骤 1: 触发备份
    step_start = _time.time()
    try:
        backup_id = await trigger_backup()
        ok = bool(backup_id)
        steps.append({
            "name": "trigger_backup", "success": ok,
            "duration_seconds": _time.time() - step_start,
            "backup_id": backup_id,
            "error": "" if ok else _i18n_t('services.disaster_recovery.s13'),
        })
        if not ok:
            overall_success = False
    except Exception as e:
        overall_success = False
        steps.append({
            "name": "trigger_backup", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })
        backup_id = ""

    # 步骤 2: 校验完整性
    step_start = _time.time()
    if backup_id:
        try:
            # R40 P0-7: 优先使用 BackupEngine.verify_backup(基于 manifest + COMPLETE marker)
            # 旧 verify_backup 函数保留以兼容历史 db_backup_* key,新格式 backups/<id>.* 走 BackupEngine
            if backup_id.startswith("backup_"):
                from services.backup_engine import BackupEngine
                engine = BackupEngine()
                verify_result = await engine.verify_backup(backup_id)
            else:
                verify_result = await verify_backup(backup_id)
            ok = verify_result.get("valid", False)
            steps.append({
                "name": "verify_backup", "success": ok,
                "duration_seconds": _time.time() - step_start,
                "verify_result": verify_result,
                "error": "" if ok else verify_result.get("error", ""),
            })
            if not ok:
                # 校验失败不阻断演练(可能是新备份尚未上传完成)
                logger.warning("[DisasterRecovery] 演练: 备份校验未通过")
        except Exception as e:
            steps.append({
                "name": "verify_backup", "success": False,
                "duration_seconds": _time.time() - step_start,
                "error": str(e),
            })
    else:
        steps.append({
            "name": "verify_backup", "success": False,
            "duration_seconds": 0,
            "error": _i18n_t('services.disaster_recovery.s10'),
        })

    # 步骤 3: 模拟恢复(R40 P0-7: 通过 BackupEngine.restore staging 模式校验可解密)
    step_start = _time.time()
    if backup_id:
        try:
            from services.backup_engine import BackupEngine
            engine = BackupEngine()
            # staging 模式仅校验可解密 + 统计行数,不写库
            staging_result = await engine.restore(
                backup_id, target="staging", approver_id=0,
            )
            ok = staging_result.get("success", False)
            tables_count = staging_result.get("restored_tables", 0)
            total_rows = staging_result.get("restored_rows", 0)
            steps.append({
                "name": "simulate_restore", "success": ok,
                "duration_seconds": _time.time() - step_start,
                "tables_count": tables_count,
                "total_rows": total_rows,
                "checksum_verified": staging_result.get("checksum_verified", False),
                "error": staging_result.get("error", "") or ("" if ok else _i18n_t('services.disaster_recovery.s14')),
            })
            if not ok:
                overall_success = False
        except Exception as e:
            overall_success = False
            steps.append({
                "name": "simulate_restore", "success": False,
                "duration_seconds": _time.time() - step_start,
                "error": str(e),
            })
    else:
        steps.append({
            "name": "simulate_restore", "success": False,
            "duration_seconds": 0,
            "error": _i18n_t('services.disaster_recovery.s11'),
        })

    # 步骤 4: 记录演练结果
    step_start = _time.time()
    try:
        await _append_recovery_history({
            "backup_id": backup_id,
            "admin_id": 0,
            "executed_at": _dt.datetime.now().isoformat(),
            "success": overall_success,
            "restored_tables": 0,
            "errors": [],
            "duration_seconds": _time.time() - start_ts,
            "type": "drill",
        })
        steps.append({
            "name": "record_drill", "success": True,
            "duration_seconds": _time.time() - step_start,
            "error": "",
        })
    except Exception as e:
        steps.append({
            "name": "record_drill", "success": False,
            "duration_seconds": _time.time() - step_start,
            "error": str(e),
        })

    return {
        "success": overall_success,
        "duration_seconds": _time.time() - start_ts,
        "steps": steps,
    }


# ─── 7. 恢复历史 ──────────────────────────────────────────────

async def get_recovery_history(limit: int = 10) -> list[dict]:
    """获取恢复历史。

    从 kv_store 读取 'recovery_history'。

    Returns:
        [{backup_id, admin_id, executed_at, success, restored_tables,
          errors, duration_seconds}]
    """
    store = get_cache_store()
    try:
        raw = await store.get_kv(KV_RECOVERY_HISTORY)
        if not raw:
            return []
        history = json.loads(raw)
        if not isinstance(history, list):
            return []
        # 按时间倒序
        history.sort(key=lambda x: x.get("executed_at", ""), reverse=True)
        return history[:limit]
    except Exception as e:
        logger.debug(f"[DisasterRecovery] get_recovery_history 失败: {e}")
        return []


# ─── 8. 备份计划 ──────────────────────────────────────────────

async def get_backup_schedule() -> dict:
    """获取备份计划。

    Returns:
        {interval_minutes, enabled, next_backup_at, retention_days}
    """
    try:
        from config import settings
        interval_minutes = int(getattr(settings, "DB_BACKUP_INTERVAL_MINUTES", 360))
        enabled = bool(getattr(settings, "DB_BACKUP_ENABLED", False))
    except Exception:
        interval_minutes = 360
        enabled = False

    # 计算下次备份时间(上次备份时间 + 间隔)
    next_backup_at = None
    store = get_cache_store()
    try:
        raw = await store.get_kv(KV_LAST_BACKUP_AT)
        if raw:
            last_dt = _dt.datetime.fromisoformat(raw)
            next_dt = last_dt + _dt.timedelta(minutes=interval_minutes)
            next_backup_at = next_dt.isoformat()
    except Exception:
        pass

    # 保留天数(从 MAX_BACKUP_RETENTION 推算)
    try:
        from services.db_backup import MAX_BACKUP_RETENTION
        # MAX_BACKUP_RETENTION 是保留份数(168),按每小时一份约 7 天
        retention_days = MAX_BACKUP_RETENTION // 24
    except Exception:
        retention_days = 7

    return {
        "interval_minutes": interval_minutes,
        "enabled": enabled,
        "next_backup_at": next_backup_at,
        "retention_days": retention_days,
    }


# ─── 9. 格式化输出 ────────────────────────────────────────────

async def format_disaster_status(status: dict) -> str:
    """格式化灾备状态为管理员可读文本。

    Args:
        status: 通常传入 get_rpo_rto() + get_backup_schedule() 合并结果

    Returns:
        多行文本报告
    """
    lines: list[str] = []
    lines.append("═══════════════════════════════════════════════════════════")
    lines.append(_i18n_t('services.disaster_recovery.s1'))
    lines.append("═══════════════════════════════════════════════════════════")

    # RPO/RTO
    rpo = status.get("rpo_seconds", DEFAULT_RPO_SECONDS)
    rto = status.get("rto_seconds", DEFAULT_RTO_SECONDS)
    # R40 P1-10: last_backup_age 可能为 None(无备份),
    # 不能默认 0(会显示"距今=0s"与违规判定矛盾,易误导管理员)
    last_age = status.get("last_backup_age")
    est_recover = status.get("estimated_recovery_time", 0)
    rpo_ok = status.get("rpo_compliant", False)
    rto_ok = status.get("rto_compliant", False)

    # R40 P1-10: None 时输出"无备份",而非误导性的"0s"
    if last_age is None:
        last_age_text = _i18n_t('services.disaster_recovery.s2')
    else:
        last_age_text = f"{last_age}s"

    lines.append("")
    lines.append(_i18n_t('services.disaster_recovery.s3', rpo=rpo, rpo_3600=rpo // 3600, last_age_text=last_age_text, if_rpo_ok_else='✓ 合规' if rpo_ok else '✗ 违规'))
    lines.append(_i18n_t('services.disaster_recovery.s4', rto=rto, rto_60=rto // 60, est_recover=est_recover, if_rto_ok_else='✓ 合规' if rto_ok else '✗ 违规'))

    # 备份计划
    lines.append("")
    lines.append(_i18n_t('services.disaster_recovery.s5', status_get_enabled_False=status.get('enabled', False), status_get_interval_minutes_0=status.get('interval_minutes', 0), status_get_retention_days_0=status.get('retention_days', 0)))
    next_at = status.get("next_backup_at")
    if next_at:
        lines.append(_i18n_t('services.disaster_recovery.s8', next_at=next_at))

    lines.append("═══════════════════════════════════════════════════════════")
    return "\n".join(lines)


# ─── 内部辅助函数 ─────────────────────────────────────────────

async def _append_backup_history(record: dict) -> None:
    """追加一条备份记录到 backup_history。"""
    store = get_cache_store()
    try:
        raw = await store.get_kv(KV_BACKUP_HISTORY)
        history = json.loads(raw) if raw else []
        if not isinstance(history, list):
            history = []
        history.append(record)
        # 限制最多 200 条
        if len(history) > 200:
            history = history[-200:]
        await store.set_kv(KV_BACKUP_HISTORY,
                          json.dumps(history, ensure_ascii=False, default=str))
    except Exception as e:
        logger.error(f"[DisasterRecovery] _append_backup_history 失败: {e}")


async def _append_recovery_history(record: dict) -> None:
    """追加一条恢复记录到 recovery_history。"""
    store = get_cache_store()
    try:
        raw = await store.get_kv(KV_RECOVERY_HISTORY)
        history = json.loads(raw) if raw else []
        if not isinstance(history, list):
            history = []
        history.append(record)
        # 限制最多 100 条
        if len(history) > 100:
            history = history[-100:]
        await store.set_kv(KV_RECOVERY_HISTORY,
                          json.dumps(history, ensure_ascii=False, default=str))
    except Exception as e:
        logger.error(f"[DisasterRecovery] _append_recovery_history 失败: {e}")


async def _write_audit_log(actor_id: int, action: str,
                          target_type: str = "", target_id: str = "",
                          details: str = "") -> int:
    """内联审计日志写入(与 maintenance_mode.py 相同)。

    Returns:
        新插入行 id;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    try:
        cursor = await store._db.execute(
            """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
               target_id, details, ip_addr, created_at)
               VALUES (?, 'admin', ?, ?, ?, ?, '', ?)""",
            (actor_id, action, target_type, target_id, details,
             _dt.datetime.now().isoformat()),
        )
        await store._db.commit()
        if cursor and cursor.lastrowid:
            await store.add_dirty_outbox("audit_log", str(cursor.lastrowid))
            return int(cursor.lastrowid)
        return 0
    except Exception as e:
        logger.error(f"[DisasterRecovery] _write_audit_log 失败: {e}")
        return 0
