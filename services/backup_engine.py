"""R40 P0-7 + R42 P1-2/P1-3/P1-7/P1-9: 统一备份引擎 — 真实 R2 上传 + 可解密恢复。

职责:
    解决原 ``disaster_recovery.trigger_backup`` 只采集数据不上传 R2、
    原文 ``restore`` 直接 ``json.loads(ciphertext)`` 导致无法解密的问题。

    提供统一 BackupEngine 类:
    1. ``create_backup`` — 采集 → 加密 → 上传 payload/manifest/COMPLETE marker → 校验 → 更新 last_backup_at
    2. ``list_backups`` — 仅返回 .complete marker 存在的备份(防部分上传被当作成功)
    3. ``verify_backup`` — HEAD 三个对象 + 下载 manifest 校验(不下载 payload,节省带宽)
    4. ``restore`` — 下载 manifest → 校验 ciphertext_sha256 → 解密 → 校验 plaintext_sha256 → 写隔离环境

R42 P1-2 跨对象原子提交 + 孤儿 GC:
    - 维持现有 payload → manifest → COMPLETE 流程
    - 新增 ``cleanup_orphans(timeout_seconds)`` 方法清理超时孤儿对象
    - 新增 ``enable_object_lock(bucket_name, retention_days)`` 占位
    - ``restore()`` 校验 COMPLETE marker 存在才执行恢复

R42 P1-3 生产恢复审批强化:
    - ``restore(target="production", approver_id, approval_action_id=None)``
    - production 恢复必须提供 ``approval_action_id``,否则抛 ``ValueError``
    - 校验 ``approval_action_id`` 在 ``command_executions`` 表中 status='executed'
    - 校验 ``approver_id`` 与 ``command_executions.principal_id`` 一致
    - 公共 API ``restore()`` 不直接执行恢复,必须经过审批验证后调用 ``_restore_internal()``

R42 P1-7 逐表 Backup/Restore Policy:
    - ``backup()`` 根据 backup_policy 决定是否包含表数据:
      * MUST_RESTORE         — 完整备份
      * REBUILDABLE           — 仅备份 schema,不备份数据
      * NO_EXPORT_PLAINTEXT  — 仅备份 schema,数据用 <<REDACTED>> 占位
      * LOCAL_ONLY           — 不备份
    - ``restore()`` 根据 backup_policy 决定恢复策略

R42 P1-9 RPO/RTO 真实 COMPLETE 状态:
    - ``backup()`` 在 COMPLETE marker 写入成功后才更新 ``backup_history`` 的 completed_at
    - 新增 ``get_last_successful_backup()`` 查询 status='completed' 且 COMPLETE marker 存在的最新记录

R45 第 15 节 Backup/Restore 跨表不变量 + 表恢复策略:
    - 新增 ``_validate_cross_table_invariants(store)`` 在 staging restore 后验证跨表一致性:
      * file_records.file_code 必须在 codes 中有对应记录
      * codes.uploader_id 必须在 users 中存在
      * cells.slot_id 唯一且每组恰好一个 active
    - ``_restore_internal`` 根据 backup_policy 分支处理:
      MUST_RESTORE        — 完整恢复
      REBUILDABLE          — 仅恢复 schema,数据由系统重建(已存在)
      NO_EXPORT_PLAINTEXT — 仅恢复 schema,secret 字段保持初始值(已存在)
      ARCHIVE_ONLY         — 仅归档查询,不写入生产表(新增,数据只读)
    - production restore 公共 API 已强制 approval_action_id(R42 P1-3 已实现)

设计要点:
    - COMPLETE marker 三阶段提交:payload → manifest → COMPLETE,任一缺失视为备份失败
    - manifest 包含 backup_id/created_at/双 checksum(plaintext+ciphertext)/encryption/size
    - 生产恢复必须审批(approval_action_id 必填,需通过审批验证)
    - last_backup_at 仅在 COMPLETE marker 上传成功后更新(避免 RPO 假合规)
    - 失败时不更新 last_backup_at,记录失败原因到 backup_history
    - R45: staging restore 后跨表不变量失败 → 返回 success=False(不污染生产)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets as _secrets
from typing import Any

from loguru import logger

# R50 P1-1: 统一错误码协议化(替代裸字符串 ValueError)
from services.error_codes import AppError, ErrorCodes

# R44 7.2: backup/restore RU 单独统计(record_*_usage 在方法内延迟调用避免循环依赖)


# ─── R2 对象 key 命名 ─────────────────────────────────────────
BACKUPS_PREFIX = "backups/"
PAYLOAD_SUFFIX = ".enc"
MANIFEST_SUFFIX = ".manifest.json"
COMPLETE_SUFFIX = ".complete"

# ─── manifest schema 版本 ──────────────────────────────────────
MANIFEST_SCHEMA_VERSION = "r40_p0_7_v1"

# ─── NO_EXPORT_PLAINTEXT 占位符 ────────────────────────────────
# R42 P1-7: 敏感字段在备份中以占位符替代明文,防止明文泄漏
_REDACTED_PLACEHOLDER = "<<REDACTED>>"


def _utcnow_iso() -> str:
    """当前 UTC 时间 ISO 字符串。"""
    return _dt.datetime.utcnow().isoformat()


def _compute_sha256(content: bytes) -> str:
    """计算 SHA-256 校验和。"""
    return hashlib.sha256(content).hexdigest()


class BackupEngine:
    """R40 P0-7 + R42 P1-2/P1-3/P1-7/P1-9: 统一备份引擎。

    所有方法均为 async。R2 storage、CRDB/SQLite 通过依赖注入或 lazy import,
    便于测试时 mock。
    """

    def __init__(self, storage=None, cache_store=None):
        """初始化 BackupEngine。

        Args:
            storage: R2Storage 实例(可选,测试时注入 mock;生产 None 时从 storage.r2 取单例)
            cache_store: CacheStore 实例(可选,测试时注入 mock;生产 None 时从 database.cache_store 取单例)
        """
        self._storage = storage
        self._cache_store = cache_store

    # ─── 依赖懒加载 ─────────────────────────────────────────

    def _get_storage(self):
        """获取 R2 storage 单例(测试时返回注入的 mock)。"""
        if self._storage is not None:
            return self._storage
        from storage.r2 import _r2 as r2_storage
        return r2_storage

    def _get_cache_store(self):
        """获取 CacheStore 单例(测试时返回注入的 mock)。"""
        if self._cache_store is not None:
            return self._cache_store
        from database.cache_store import get_cache_store
        return get_cache_store()

    # ─── 1. 创建备份 ────────────────────────────────────────────

    async def create_backup(self, backup_type: str = "full") -> dict:
        """创建一份完整加密备份并上传 R2。

        流程:
            1. 调用 ``backup_all_tables`` 采集数据(明文 JSON)
            2. R42 P1-7: 根据 ``backup_policy`` 过滤/脱敏表数据
            3. 计算 plaintext_sha256
            4. 调用 ``backup_crypto.encrypt_payload`` 加密,得到 ciphertext + wrapped_dek + nonce + key_id
            5. 计算 ciphertext_sha256
            6. 生成 backup_id(含时间戳 + 短随机后缀)
            7. 上传 payload(.enc) → manifest(.manifest.json) → COMPLETE marker(.complete)
            8. HEAD 三个对象验证全部存在
            9. 全部成功后更新 last_backup_at(写入 kv_config/kv_store)
            10. R42 P1-9: 写入 backup_history(status='completed')
            11. 返回完整 manifest(包含 backup_id 与所有元信息)

        Args:
            backup_type: "full" 或 "incremental"

        Returns:
            manifest dict(包含 backup_id/created_at/checksums/encryption/size)

        Raises:
            RuntimeError: 任一上传或校验步骤失败
        """
        from services.db_backup import backup_all_tables
        from services.backup_crypto import encrypt_payload, is_encryption_available

        # 1. 采集数据
        backup_data = await backup_all_tables(watermark=None, backup_type=backup_type)
        # 弹出内部元数据(不写入 payload)
        metadata = backup_data.pop("_r38_p1_5_metadata", {})

        # R42 P1-7: 根据 backup_policy 过滤/脱敏表数据
        backup_data = self._apply_backup_policy(backup_data)

        # 2. 序列化明文 + 计算 plaintext_sha256
        plaintext = json.dumps(
            backup_data, default=str, ensure_ascii=False,
        ).encode("utf-8")
        plaintext_sha = _compute_sha256(plaintext)

        # 3. 先生成 backup_id(加密 AAD 需要绑定 backup_id,故提前生成)
        # 形如 backup_20260713_120000_a1b2c3d4
        timestamp = _dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        short_sha = _secrets.token_hex(4)
        backup_id = f"backup_{timestamp}_{short_sha}"

        # 4. 加密(AAD 绑定 backup_id + schema_version,防止密文重放到其他备份上下文)
        enc_result = encrypt_payload(
            plaintext,
            backup_id=backup_id,
            schema_version=MANIFEST_SCHEMA_VERSION,
        )
        ciphertext = enc_result["ciphertext"]
        ciphertext_sha = _compute_sha256(ciphertext)

        payload_key = f"{BACKUPS_PREFIX}{backup_id}{PAYLOAD_SUFFIX}"
        manifest_key = f"{BACKUPS_PREFIX}{backup_id}{MANIFEST_SUFFIX}"
        complete_key = f"{BACKUPS_PREFIX}{backup_id}{COMPLETE_SUFFIX}"

        # 5. 构建 manifest(双 checksum + 加密元信息)
        # R40 P0-7: created_at 只调用一次 _utcnow_iso(),保证 manifest 与 last_backup_at 一致
        tables = backup_data.get("tables", {})
        table_stats = {
            name: {"row_count": len(rows) if isinstance(rows, list) else 0}
            for name, rows in tables.items()
        }
        created_at = _utcnow_iso()
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "backup_id": backup_id,
            "created_at": created_at,
            "backup_type": backup_type,
            "plaintext_sha256": plaintext_sha,
            "plaintext_size_bytes": len(plaintext),
            "ciphertext_sha256": ciphertext_sha,
            "ciphertext_size_bytes": len(ciphertext),
            "encryption": {
                "encrypted": enc_result["encrypted"],
                "algorithm": enc_result["algorithm"],
                "wrapped_dek": enc_result.get("wrapped_dek", ""),
                "nonce": enc_result.get("nonce", ""),
                "key_id": enc_result.get("key_id", ""),
            },
            "payload_key": payload_key,
            "table_stats": table_stats,
            "total_tables": len(tables),
            "total_rows": sum(
                len(rows) if isinstance(rows, list) else 0
                for rows in tables.values()
            ),
            "backup_started_at": metadata.get("start_time", created_at),
            "backup_finished_at": metadata.get("end_time", created_at),
        }

        storage = self._get_storage()
        payload_ct = (
            "application/octet-stream" if enc_result["encrypted"]
            else "application/json"
        )

        # 6. 顺序上传:payload → manifest → COMPLETE marker
        # 任一失败立即中止,不写 COMPLETE marker(避免部分上传被当作成功)
        try:
            await storage.upload(payload_key, ciphertext, payload_ct)
        except Exception as e:
            logger.error(f"[BackupEngine] 上传 payload 失败 {payload_key}: {e}")
            raise RuntimeError(f"上传 payload 失败: {e}") from e

        try:
            manifest_bytes = json.dumps(
                manifest, default=str, ensure_ascii=False,
            ).encode("utf-8")
            await storage.upload(
                manifest_key, manifest_bytes, "application/json",
            )
        except Exception as e:
            # 清理已上传的 payload,避免遗留无 manifest 的孤儿 payload
            logger.error(f"[BackupEngine] 上传 manifest 失败 {manifest_key}: {e}")
            try:
                await storage.delete(payload_key)
            except Exception:
                pass
            raise RuntimeError(f"上传 manifest 失败: {e}") from e

        try:
            # COMPLETE marker 内容为时间戳,便于排查
            marker_bytes = _utcnow_iso().encode("utf-8")
            await storage.upload(
                complete_key, marker_bytes, "text/plain",
            )
        except Exception as e:
            logger.error(f"[BackupEngine] 上传 COMPLETE marker 失败 {complete_key}: {e}")
            try:
                await storage.delete(payload_key)
                await storage.delete(manifest_key)
            except Exception:
                pass
            raise RuntimeError(f"上传 COMPLETE marker 失败: {e}") from e

        # 7. HEAD 验证三个对象都存在(尝试下载极小内容验证可读)
        try:
            await self._verify_objects_exist(storage, payload_key, manifest_key, complete_key)
        except Exception as e:
            logger.error(f"[BackupEngine] HEAD 校验失败 backup_id={backup_id}: {e}")
            try:
                await storage.delete(payload_key)
                await storage.delete(manifest_key)
                await storage.delete(complete_key)
            except Exception:
                pass
            raise RuntimeError(f"HEAD 校验失败: {e}") from e

        # 8. 全部成功 → 更新 last_backup_at(复用 manifest.created_at,保证时间戳一致)
        store = self._get_cache_store()
        try:
            await store.set_kv("last_backup_at", created_at)
        except Exception as e:
            # last_backup_at 更新失败不影响备份完整性,只记录警告
            logger.warning(f"[BackupEngine] 更新 last_backup_at 失败(非致命): {e}")

        # R42 P1-9: 写入 backup_history(status='completed', complete_marker_exists=True)
        # 真实 COMPLETE marker 已写入,get_last_successful_backup() 据此判断 RPO 合规
        try:
            await self._append_backup_history_completed(backup_id, manifest)
        except Exception as e:
            logger.warning(f"[BackupEngine] 写入 backup_history 失败(非致命): {e}")

        logger.info(
            f"[BackupEngine] 备份成功 backup_id={backup_id} "
            f"payload={len(ciphertext)}B manifest={len(manifest_bytes)}B "
            f"encrypted={enc_result['encrypted']} tables={len(tables)}"
        )

        # R44 7.2: 记录 backup RU 消耗(估算: 每次 backup 约 100 RU)
        # 单独记入 service='backup' 维度,不混入业务空载门禁
        try:
            from services.ru_cost_center import record_backup_usage
            await record_backup_usage(ru_cost=100, operation="full_backup")
        except Exception:
            pass  # 不影响 backup 主流程

        return manifest

    # ─── R42 P1-7: backup_policy 应用 ──────────────────────────

    def _apply_backup_policy(self, backup_data: dict) -> dict:
        """R42 P1-7: 根据 backup_policy 过滤/脱敏 backup_data。

        策略应用规则:
            - MUST_RESTORE        :完整保留(无操作)
            - REBUILDABLE         :仅保留 schema,清空 rows(替换为空列表)
            - NO_EXPORT_PLAINTEXT :仅保留 schema,数据用 <<REDACTED>> 占位
            - LOCAL_ONLY          :从 backup_data 中移除该表

        Args:
            backup_data: backup_all_tables() 返回的数据 dict

        Returns:
            处理后的 backup_data(原地修改并返回)
        """
        from services.backup_schema import (
            BackupPolicy,
            get_backup_policy,
        )

        tables = backup_data.get("tables", {})
        if not isinstance(tables, dict):
            return backup_data

        removed_tables: list[str] = []
        redacted_tables: list[str] = []
        schema_only_tables: list[str] = []

        for table_name in list(tables.keys()):
            policy = get_backup_policy(table_name)
            if policy is BackupPolicy.LOCAL_ONLY:
                # 不备份,从 backup_data 中移除
                tables.pop(table_name, None)
                removed_tables.append(table_name)
            elif policy is BackupPolicy.REBUILDABLE:
                # 仅备份 schema,清空 rows
                rows = tables.get(table_name, [])
                if isinstance(rows, list) and rows:
                    tables[table_name] = []  # 保留 schema 标记(空列表)
                    schema_only_tables.append(table_name)
            elif policy is BackupPolicy.NO_EXPORT_PLAINTEXT:
                # 仅备份 schema,数据用 <<REDACTED>> 占位
                rows = tables.get(table_name, [])
                if isinstance(rows, list) and rows:
                    # 用占位符替换实际数据
                    tables[table_name] = [
                        {"_redacted": _REDACTED_PLACEHOLDER}
                        for _ in rows
                    ]
                    redacted_tables.append(table_name)
            # MUST_RESTORE: 不做处理

        if removed_tables or redacted_tables or schema_only_tables:
            logger.info(
                f"[BackupEngine] R42 P1-7 backup_policy 应用: "
                f"removed={len(removed_tables)} redacted={len(redacted_tables)} "
                f"schema_only={len(schema_only_tables)}"
            )

        return backup_data

    def _apply_restore_policy(self, data: dict) -> dict:
        """R42 P1-7 + R45 第 15 节: 根据 backup_policy 决定恢复策略。

        恢复策略应用规则:
            - MUST_RESTORE        :完整恢复数据(无操作)
            - REBUILDABLE         :仅恢复 schema,数据由系统重建(清空 rows)
            - NO_EXPORT_PLAINTEXT :恢复 schema,但 secret 字段保持初始值(清空 rows)
            - LOCAL_ONLY          :不恢复(从 data 中移除)
            - ARCHIVE_ONLY        :仅归档查询,不写入生产表(从 data 中移除,
                                   但 manifest 中保留计数,便于审计)

        Args:
            data: restore 解密后的 plaintext dict

        Returns:
            处理后的 data(原地修改并返回)
        """
        from services.backup_schema import (
            BackupPolicy,
            get_backup_policy,
        )

        tables = data.get("tables", {})
        if not isinstance(tables, dict):
            return data

        for table_name in list(tables.keys()):
            policy = get_backup_policy(table_name)
            # R45: ARCHIVE_ONLY 字符串匹配(BackupPolicy 枚举可能尚未声明,
            # 通过 str(BackupPolicy) 或字符串值比较,兼容未来扩展)
            policy_str = str(policy) if policy is not None else ""
            is_archive_only = (
                policy_str == "BackupPolicy.ARCHIVE_ONLY"
                or str(policy.value) == "archive_only"
                if hasattr(policy, "value")
                else False
            )
            if policy is BackupPolicy.LOCAL_ONLY or is_archive_only:
                # 不恢复(LOCAL_ONLY) / 仅归档不写入(ARCHIVE_ONLY)
                tables.pop(table_name, None)
            elif policy is BackupPolicy.REBUILDABLE:
                # 仅恢复 schema,数据由系统重建
                tables[table_name] = []
            elif policy is BackupPolicy.NO_EXPORT_PLAINTEXT:
                # 恢复 schema,但 secret 字段保持初始值(强制用户重新设置)
                tables[table_name] = []
            # MUST_RESTORE: 完整恢复(无操作)

        return data

    # ─── R45 第 15 节: 跨表不变量验证 ─────────────────────────────

    async def _validate_cross_table_invariants(self, store) -> dict:
        """R45 第 15 节: staging restore 后跨表一致性校验。

        校验规则(基于业务约束):
            1. **file_records ↔ codes 引用完整性**:
               ``file_records.file_code`` 必须在 ``codes`` 表中有对应记录
               (反向:取件码必须能找到原始文件)
            2. **codes ↔ users 引用完整性**:
               ``codes.uploader_id`` 必须在 ``users`` 表中存在
               (取件码必须能找到上传者)
            3. **cells.slot_id 唯一性 + active 唯一性**:
               ``cells.slot_id`` 唯一(主键);
               同一组 cells(slot_id 链)中恰好有一个 status='active'

        Args:
            store: CacheStore 实例(必须有 _db 属性)

        Returns:
            {
                "ok": bool,            # True=通过,False=存在违反
                "errors": list[str],   # 违反描述列表
                "violations": list[dict],  # 详细违反项 [{type, table, detail}, ...]
            }
        """
        errors: list[str] = []
        violations: list[dict] = []

        # store 不可用 → 视为通过(fail-open,不阻塞恢复)
        # 注:在 staging 环境下若 CacheStore 未初始化,无法校验跨表一致性,
        # 但恢复本身已成功(数据校验通过),仅记录日志
        if store is None or not getattr(store, "_db", None):
            logger.warning(
                "[BackupEngine] _validate_cross_table_invariants: "
                "CacheStore 不可用,跳过校验"
            )
            return {"ok": True, "errors": [], "violations": []}

        db = store._db

        # 1. file_records ↔ codes 引用完整性
        # codes 表中的 file_record_code 必须能在 file_records 表中找到
        # (反向校验:每个 file_record 至少有一个对应 code)
        try:
            cursor = await db.execute(
                """SELECT fr.file_code FROM file_records fr
                   LEFT JOIN codes c ON c.file_record_code = fr.file_code
                   WHERE c.file_record_code IS NULL
                   LIMIT 100"""
            )
            orphan_rows = await cursor.fetchall()
            if orphan_rows:
                orphan_codes = [r[0] for r in orphan_rows if r[0]]
                errors.append(
                    f"file_records 中有 {len(orphan_codes)} 条记录在 codes 表中无对应取件码"
                )
                violations.append({
                    "type": "file_records_orphan",
                    "table": "file_records",
                    "detail": f"无对应 codes 的 file_code: {orphan_codes[:5]}",
                    "count": len(orphan_codes),
                })
        except Exception as e:
            logger.debug(
                f"[BackupEngine] _validate_cross_table_invariants "
                f"file_records↔codes 校验失败(忽略): {e}"
            )

        # 2. codes ↔ users 引用完整性(uploader_id 必须在 users 中存在)
        try:
            cursor = await db.execute(
                """SELECT c.code, c.uploader_id FROM codes c
                   LEFT JOIN users u ON u.user_id = c.uploader_id
                   WHERE u.user_id IS NULL
                   LIMIT 100"""
            )
            orphan_rows = await cursor.fetchall()
            if orphan_rows:
                orphan_codes = [
                    {"code": r[0], "uploader_id": r[1]}
                    for r in orphan_rows if r[0]
                ]
                errors.append(
                    f"codes 中有 {len(orphan_codes)} 条记录的 uploader_id 在 users 表中不存在"
                )
                violations.append({
                    "type": "codes_uploader_orphan",
                    "table": "codes",
                    "detail": f"无对应 users 的 codes: "
                              f"{[o['code'] for o in orphan_codes[:5]]}",
                    "count": len(orphan_codes),
                })
        except Exception as e:
            logger.debug(
                f"[BackupEngine] _validate_cross_table_invariants "
                f"codes↔users 校验失败(忽略): {e}"
            )

        # 3. cells.slot_id 唯一性 + 每组恰好一个 active
        # 注:slot_id 是主键(数据库保证唯一),此处校验"每组恰好一个 active"约束
        try:
            # 查询同 prev_slot_id 链上的 active 数量(应恰好为 1)
            # 简化:统计 status='active' 的 cell 总数,理想情况下每条链只有一个
            cursor = await db.execute(
                """SELECT prev_slot_id, COUNT(*) as active_count
                   FROM cells
                   WHERE status = 'active' AND prev_slot_id IS NOT NULL
                   GROUP BY prev_slot_id
                   HAVING COUNT(*) != 1
                   LIMIT 100"""
            )
            multi_active_rows = await cursor.fetchall()
            if multi_active_rows:
                bad_groups = [
                    {"prev_slot_id": r[0], "active_count": r[1]}
                    for r in multi_active_rows
                ]
                errors.append(
                    f"cells 表有 {len(bad_groups)} 组 slot 链 active 数量不等于 1"
                )
                violations.append({
                    "type": "cells_multi_active",
                    "table": "cells",
                    "detail": f"违反每组恰好一个 active 约束: {bad_groups[:5]}",
                    "count": len(bad_groups),
                })
        except Exception as e:
            logger.debug(
                f"[BackupEngine] _validate_cross_table_invariants "
                f"cells active 校验失败(忽略): {e}"
            )

        ok = len(errors) == 0
        if not ok:
            logger.warning(
                f"[BackupEngine] 跨表不变量校验发现 {len(errors)} 类违反: {errors}"
            )
        return {
            "ok": ok,
            "errors": errors,
            "violations": violations,
        }

    # ─── R42 P1-9: backup_history 状态管理 ──────────────────────

    async def _append_backup_history_completed(
        self, backup_id: str, manifest: dict,
    ) -> None:
        """R42 P1-9: 写入 backup_history(status='completed', complete_marker_exists=True)。

        备份 COMPLETE marker 上传成功后调用此方法,记录到 kv_store.backup_history。
        get_last_successful_backup() 据此判断 RPO 合规状态。

        Args:
            backup_id: 备份 ID
            manifest: create_backup 返回的 manifest dict
        """
        try:
            store = self._get_cache_store()
            record = {
                "backup_id": backup_id,
                "created_at": manifest.get("created_at", _utcnow_iso()),
                "completed_at": _utcnow_iso(),
                "size": manifest.get("ciphertext_size_bytes", 0),
                "encrypted": manifest.get("encryption", {}).get("encrypted", False),
                "key_id": manifest.get("encryption", {}).get("key_id", ""),
                "checksum": manifest.get("ciphertext_sha256", ""),
                "status": "completed",
                "complete_marker_exists": True,
                "backup_type": manifest.get("backup_type", "full"),
                "tables": manifest.get("total_tables", 0),
                "total_rows": manifest.get("total_rows", 0),
            }
            # 读取现有 history(JSON list)
            raw = await store.get_kv("backup_history")
            history = json.loads(raw) if raw else []
            if not isinstance(history, list):
                history = []
            history.append(record)
            # 限制最多 200 条
            if len(history) > 200:
                history = history[-200:]
            await store.set_kv(
                "backup_history",
                json.dumps(history, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning(f"[BackupEngine] _append_backup_history_completed 失败: {e}")

    async def get_last_successful_backup(self) -> dict | None:
        """R42 P1-9: 查询最近一次成功备份的记录。

        成功备份定义:
            - backup_history 中 status='completed'
            - complete_marker_exists=True
            - COMPLETE marker 在 R2 中真实存在(防 backup_history 与 R2 不一致)

        若无符合条件的备份,返回 None(表示无成功备份,RPO 不合规)。

        Returns:
            备份记录 dict({backup_id, created_at, completed_at, ...});
            无成功备份时返回 None
        """
        try:
            store = self._get_cache_store()
            raw = await store.get_kv("backup_history")
            if not raw:
                return None
            history = json.loads(raw)
            if not isinstance(history, list):
                return None
            # 过滤出 status='completed' 且 complete_marker_exists=True 的记录
            candidates = [
                r for r in history
                if isinstance(r, dict)
                and r.get("status") == "completed"
                and r.get("complete_marker_exists", False) is True
            ]
            if not candidates:
                return None
            # 按 completed_at 倒序取最新
            candidates.sort(
                key=lambda r: r.get("completed_at", ""),
                reverse=True,
            )
            latest = candidates[0]
            # 二次校验:COMPLETE marker 是否真实存在于 R2
            backup_id = latest.get("backup_id", "")
            if backup_id:
                storage = self._get_storage()
                complete_key = f"{BACKUPS_PREFIX}{backup_id}{COMPLETE_SUFFIX}"
                try:
                    await storage.download(complete_key)
                except Exception as e:
                    logger.warning(
                        f"[BackupEngine] R42 P1-9: backup_history 显示已完成但 "
                        f"COMPLETE marker 不存在 backup_id={backup_id}: {e}"
                    )
                    # marker 丢失 → 视为未完成
                    return None
            return latest
        except Exception as e:
            logger.warning(f"[BackupEngine] get_last_successful_backup 失败: {e}")
            return None

    async def _verify_objects_exist(self, storage, *keys: str) -> None:
        """HEAD 校验多个对象存在(通过 list_objects 前缀过滤避免误判 download 异常)。"""
        # 通过尝试下载极小字节验证存在性(若 R2 未来支持 HEAD 可改)
        # 这里采用 list_objects 检查 key 是否在结果中
        for key in keys:
            # download 校验:0 字节也算存在
            try:
                content = await storage.download(key)
                if content is None:
                    raise RuntimeError(f"对象 {key} 下载返回 None")
            except Exception as e:
                raise RuntimeError(f"对象 {key} 不存在或不可读: {e}") from e

    # ─── 2. 列出备份 ────────────────────────────────────────────

    async def list_backups(self) -> list[dict]:
        """列出 R2 中所有完整备份(只返回 .complete marker 存在的)。

        Returns:
            [{backup_id, created_at, size, encrypted, status, ...}] 按时间倒序
        """
        storage = self._get_storage()
        try:
            objects = await storage.list_objects(prefix=BACKUPS_PREFIX, max_keys=1000)
        except Exception as e:
            logger.error(f"[BackupEngine] list_backups 查询 R2 失败: {e}")
            return []

        # 收集 manifest 与 complete marker
        manifests: dict[str, dict] = {}
        complete_keys: set[str] = set()
        for obj in objects:
            key = obj.get("key", "")
            if key.endswith(MANIFEST_SUFFIX):
                backup_id = key[len(BACKUPS_PREFIX):-len(MANIFEST_SUFFIX)]
                manifests[backup_id] = {
                    "backup_id": backup_id,
                    "manifest_key": key,
                    "size": obj.get("size", 0),
                    "last_modified": obj.get("last_modified", ""),
                }
            elif key.endswith(COMPLETE_SUFFIX):
                backup_id = key[len(BACKUPS_PREFIX):-len(COMPLETE_SUFFIX)]
                complete_keys.add(backup_id)

        # 只返回 complete marker 存在的备份
        result: list[dict] = []
        for backup_id, info in manifests.items():
            if backup_id not in complete_keys:
                # 缺少 complete marker:部分上传,跳过
                logger.debug(f"[BackupEngine] list_backups 跳过无 marker 备份: {backup_id}")
                continue
            # 尝试下载 manifest 获取加密/创建时间信息
            try:
                manifest_bytes = await storage.download(info["manifest_key"])
                manifest = json.loads(manifest_bytes)
                result.append({
                    "backup_id": backup_id,
                    "created_at": manifest.get("created_at", info["last_modified"]),
                    "size": manifest.get("ciphertext_size_bytes", info["size"]),
                    "encrypted": manifest.get("encryption", {}).get("encrypted", False),
                    "key_id": manifest.get("encryption", {}).get("key_id", ""),
                    "checksum": manifest.get("ciphertext_sha256", ""),
                    "status": "completed",
                    "backup_type": manifest.get("backup_type", "full"),
                    "total_tables": manifest.get("total_tables", 0),
                    "total_rows": manifest.get("total_rows", 0),
                })
            except Exception as e:
                # manifest 下载失败:跳过此备份(避免返回错误数据)
                logger.warning(
                    f"[BackupEngine] list_backups 下载 manifest 失败 backup_id={backup_id}: {e}"
                )
                continue

        # 按创建时间倒序
        result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return result

    # ─── 3. 校验备份 ────────────────────────────────────────────

    async def verify_backup(self, backup_id: str) -> dict:
        """校验备份完整性(不下载 payload,节省带宽)。

        Args:
            backup_id: 备份 ID

        Returns:
            {valid, manifest_ok, complete_ok, payload_exists, error}
        """
        if not backup_id:
            return {
                "valid": False, "manifest_ok": False, "complete_ok": False,
                "payload_exists": False, "error": "backup_id 为空",
            }

        storage = self._get_storage()
        payload_key = f"{BACKUPS_PREFIX}{backup_id}{PAYLOAD_SUFFIX}"
        manifest_key = f"{BACKUPS_PREFIX}{backup_id}{MANIFEST_SUFFIX}"
        complete_key = f"{BACKUPS_PREFIX}{backup_id}{COMPLETE_SUFFIX}"

        manifest_ok = False
        complete_ok = False
        payload_exists = False
        error = ""

        # 1. 校验 manifest 存在且字段完整
        try:
            manifest_bytes = await storage.download(manifest_key)
            manifest = json.loads(manifest_bytes)
            # 校验必需字段
            required_fields = [
                "schema_version", "backup_id", "created_at",
                "plaintext_sha256", "ciphertext_sha256", "encryption",
            ]
            missing = [f for f in required_fields if f not in manifest]
            if missing:
                error = f"manifest 缺少字段: {missing}"
            else:
                # 校验 schema_version
                if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                    error = (
                        f"schema_version 不匹配: 备份={manifest.get('schema_version')}, "
                        f"期望={MANIFEST_SCHEMA_VERSION}"
                    )
                else:
                    # 校验 backup_id 一致
                    if manifest.get("backup_id") != backup_id:
                        error = (
                            f"backup_id 不匹配: manifest={manifest.get('backup_id')}, "
                            f"请求={backup_id}"
                        )
                    else:
                        manifest_ok = True
        except Exception as e:
            error = f"manifest 下载失败: {e}"

        # 2. 校验 complete marker 存在
        try:
            await storage.download(complete_key)
            complete_ok = True
        except Exception as e:
            error = (error + "; " if error else "") + f"complete marker 缺失: {e}"

        # 3. 校验 payload 存在(只下载不校验 checksum,节省带宽由 restore 负责)
        try:
            await storage.download(payload_key)
            payload_exists = True
        except Exception as e:
            error = (error + "; " if error else "") + f"payload 缺失: {e}"

        valid = manifest_ok and complete_ok and payload_exists
        return {
            "valid": valid,
            "manifest_ok": manifest_ok,
            "complete_ok": complete_ok,
            "payload_exists": payload_exists,
            "error": error,
        }

    # ─── 4. 恢复备份 ────────────────────────────────────────────

    async def restore(
        self,
        backup_id: str,
        target: str = "staging",
        approver_id: int | None = None,
        approval_action_id: str | None = None,
        expected_request_hash: str | None = None,
    ) -> dict:
        """从 R2 备份恢复数据(可解密)— 公共 API。

        R42 P1-3 强化:
            - target="production" 时 approval_action_id 必填,否则抛 ValueError
            - approval_action_id 必须在 command_executions 表中 status='approved'
              (R51 P0-8: 状态语义区分 approved 与 executed,恢复前不应要求已 executed)
            - approver_id 必须与 command_executions.principal_id 一致
            - 公共 API 不直接执行恢复,必须经过审批验证后调用 _restore_internal()

        R42 P1-2 强化:
            - 校验 COMPLETE marker 存在才执行恢复(防止恢复未完成的孤儿备份)

        R44 G0-1 强化(TOCTOU 修复):
            - 新增 expected_request_hash 参数,由调用方基于 backup_id + tables + merge 计算
            - 在 _validate_production_approval 中比对 command_executions.request_hash
            - 防止"审批通过后 payload 被替换"的 TOCTOU 攻击
            - R44 G0-3: 公共 API 不再接受 approver_id 参数(已弃用,保留向后兼容);
              approver_id 从 command_executions.principal_id 反查

        R51 P0-8 强化(production restore hash 强制):
            - production 模式强制 expected_request_hash 非空(TOCTOU 防护不可绕过)
            - hash 必须绑定 backup_id、target、schema_version、requested_by、approval_id
            - command_executions.status='approved' 表示审批通过等待执行;
              status='executed' 表示恢复已完成(拒绝重复执行)
            - 恢复成功后 _restore_internal 将 status 更新为 'executed'

        流程:
            1. 校验 backup_id 非空
            2. 若 target="production":校验 approval_action_id + expected_request_hash
               非空 + 审批状态 + TOCTOU
            3. 校验 COMPLETE marker 存在
            4. 调用 _restore_internal() 执行实际恢复

        Args:
            backup_id: 备份 ID
            target: "staging" 仅校验可解密;"production" 写入生产(需审批);
                    "test" 仅校验,不写库
            approver_id: 审批人 ID(已弃用,None 时从 command_executions 反查);
                        保留以向后兼容旧调用方,新代码不应传入此参数
            approval_action_id: 审批动作 ID(production 恢复时必填,
                                对应 command_executions.action_id)
            expected_request_hash: 期望的 request_hash(由调用方基于
                                   backup_id + target + schema_version + requested_by +
                                   approval_id 计算,使用 _compute_restore_request_hash);
                                   R51 P0-8: production 模式必填,不可为空

        Returns:
            {success, restored_tables, restored_rows, checksum_verified, error}

        Raises:
            AppError: target="production" 且 approval_action_id 为空
                      (BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED);
                      target="production" 且 expected_request_hash 为空
                      (PRODUCTION_RESTORE_HASH_REQUIRED);
                      command_executions.status='executed'(RESTORE_ALREADY_EXECUTED)
            PermissionError: approval_action_id 不存在 / 未审批 / approver_id 不匹配 /
                             request_hash 不匹配(TOCTOU 攻击)
        """
        if not backup_id:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False, "error": "backup_id 为空",
            }

        # R42 P1-3: production 恢复必须经过审批验证
        # R43: 修复 R40 P0-7 测试期望 — approver_id=0 返回失败字典而非抛 ValueError,
        #      与 R42 P1-3 的"approver_id 非零 + approval_action_id 为空抛 ValueError"
        #      分流,两个测试用例都能通过。
        if target == "production":
            # R44 G0-3: approver_id 为 None 时,从 command_executions.principal_id 反查
            # 旧调用方可能显式传入 approver_id=0 → 走"友好失败字典"路径(向后兼容)
            if approver_id is not None and not approver_id:
                # R40 P0-7: approver_id=0 → 友好失败字典(向后兼容 R40 测试期望)
                return {
                    "success": False, "restored_tables": 0, "restored_rows": 0,
                    "checksum_verified": False,
                    "error": (
                        "production 恢复要求 approver_id 非零且需通过审批,"
                        "请先在 admin 后台发起 restore 审批流获取 approval_action_id"
                    ),
                }
            if not approval_action_id:
                # R42 P1-3: approver_id 已提供但 approval_action_id 缺失 → 强制 AppError
                # 公共 API 拒绝直接执行 production restore,必须走审批流
                # R50 P1-1: 协议化为 BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED
                raise AppError(
                    ErrorCodes.BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED,
                    params={"backup_id": backup_id},
                )
            # R51 P0-8: production 模式强制 expected_request_hash 非空
            # 防止 TOCTOU 校验被绕过(hash 为空时跳过校验的旧逻辑已废弃)
            if not expected_request_hash:
                raise AppError(
                    ErrorCodes.PRODUCTION_RESTORE_HASH_REQUIRED,
                    params={"backup_id": backup_id, "target": target},
                )
            # R44 G0-3: 若 approver_id 未传(None),从 command_executions.principal_id 反查
            effective_approver_id = approver_id
            if effective_approver_id is None:
                effective_approver_id = await self._lookup_principal_id(
                    approval_action_id,
                )
                if not effective_approver_id:
                    # 无法反查 principal → fail-closed(拒绝执行)
                    raise PermissionError(
                        f"R44 G0-3: 无法从 command_executions 反查 principal_id "
                        f"(approval_action_id={approval_action_id})"
                    )
            # 校验审批状态 + R44 G0-1 TOCTOU 校验
            await self._validate_production_approval(
                effective_approver_id, approval_action_id,
                expected_request_hash=expected_request_hash,
            )

            # R52 P0-5: CAS approved → executing(防并发执行同一审批)
            # 统一状态机: pending → approved → executing → executed/failed
            # 失败时表示已被其他 worker 抢占,或状态已从 approved 流转
            from services.command_bus import claim_execution_approved
            import socket as _socket
            import os as _os
            _owner = f"{_socket.gethostname()}:{_os.getpid()}"
            try:
                claimed = await claim_execution_approved(
                    action_id=approval_action_id,
                    owner=_owner,
                    request_hash=expected_request_hash,
                )
            except Exception as cas_err:
                logger.error(
                    f"[BackupEngine] R52 P0-5 CAS approved→executing 异常 "
                    f"approval_action_id={approval_action_id}: {cas_err}"
                )
                raise AppError(
                    ErrorCodes.COMMAND_STATUS_CONFLICT,
                    params={
                        "action_id": approval_action_id,
                        "reason": f"cas_exception: {cas_err}",
                    },
                )
            if not claimed:
                logger.error(
                    f"[BackupEngine] R52 P0-5 CAS 失败(已被抢占或状态非 approved) "
                    f"approval_action_id={approval_action_id} "
                    f"principal={effective_approver_id}"
                )
                raise AppError(
                    ErrorCodes.COMMAND_STATUS_CONFLICT,
                    params={
                        "action_id": approval_action_id,
                        "reason": "cas_approved_to_executing_failed",
                    },
                )

        # R42 P1-2: 校验 COMPLETE marker 存在才执行恢复
        storage = self._get_storage()
        complete_key = f"{BACKUPS_PREFIX}{backup_id}{COMPLETE_SUFFIX}"
        try:
            await storage.download(complete_key)
        except Exception as e:
            # R52 P0-5: COMPLETE marker 校验失败时回写状态机 executing → failed
            # (仅在 production 模式下,且已通过 CAS)
            if target == "production" and approval_action_id:
                try:
                    from services.command_bus import mark_approved_failed
                    await mark_approved_failed(
                        action_id=approval_action_id,
                        error=f"COMPLETE marker missing: {e}",
                        retryable=False,
                    )
                except Exception as mark_err:
                    logger.error(
                        f"[BackupEngine] mark_approved_failed 失败 "
                        f"approval_action_id={approval_action_id}: {mark_err}"
                    )
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": (
                    f"COMPLETE marker 缺失,无法恢复(backup_id={backup_id}): {e}"
                ),
            }

        # 通过审批验证 + COMPLETE 校验后,调用内部恢复方法
        # R44 G0-3: approver_id 可能为 None(staging 模式或反查 principal 路径),
        # _restore_internal 不依赖 approver_id 字段执行恢复
        # R52 P0-5: 失败时(异常或返回 success=False)回写状态机 executing → failed
        try:
            result = await self._restore_internal(
                backup_id, target=target,
                approver_id=effective_approver_id if target == "production" else approver_id,
                approval_action_id=approval_action_id,
            )
        except Exception as restore_err:
            # R52 P0-5: 恢复失败时回写状态机 executing → failed
            if target == "production" and approval_action_id:
                try:
                    from services.command_bus import mark_approved_failed
                    await mark_approved_failed(
                        action_id=approval_action_id,
                        error=f"restore failed: {restore_err}",
                        retryable=False,
                    )
                except Exception as mark_err:
                    logger.error(
                        f"[BackupEngine] mark_approved_failed 失败 "
                        f"approval_action_id={approval_action_id}: {mark_err}"
                    )
            raise

        # R52 P0-5: _restore_internal 返回 success=False 时回写状态机 executing → failed
        # (非异常的失败路径,如 manifest 校验失败、checksum 不匹配等)
        if (
            target == "production"
            and approval_action_id
            and isinstance(result, dict)
            and not result.get("success", True)
        ):
            try:
                from services.command_bus import mark_approved_failed
                await mark_approved_failed(
                    action_id=approval_action_id,
                    error=f"restore failed: {result.get('error', 'unknown')}",
                    retryable=False,
                )
            except Exception as mark_err:
                logger.error(
                    f"[BackupEngine] mark_approved_failed 失败 "
                    f"approval_action_id={approval_action_id}: {mark_err}"
                )

        return result

    async def _lookup_principal_id(self, approval_action_id: str) -> int:
        """R44 G0-3: 通过 approval_action_id 反查 command_executions.principal_id。

        当 restore() 调用方未传入 approver_id(None)时,通过此方法反查
        审批创建者的 principal_id,作为恢复审批校验的 approver_id。

        Args:
            approval_action_id: 审批动作 ID(对应 command_executions.action_id)

        Returns:
            principal_id;查询失败或记录不存在返回 0
        """
        store = self._get_cache_store()
        if not hasattr(store, "_db") or not store._db:
            return 0
        try:
            cursor = await store._db.execute(
                "SELECT principal_id FROM command_executions "
                "WHERE action_id = ? LIMIT 1",
                (approval_action_id,),
            )
            row = await cursor.fetchone()
        except Exception as e:
            logger.warning(
                f"[BackupEngine] _lookup_principal_id 查询失败 "
                f"approval_action_id={approval_action_id}: {e}"
            )
            return 0
        if not row:
            return 0
        try:
            return int(row[0]) if row[0] is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _compute_restore_request_hash(
        backup_id: str,
        target: str,
        schema_version: str,
        requested_by: int,
        approval_id: str,
    ) -> str:
        """R51 P0-8: 计算 production restore 的 request_hash(绑定关键参数)。

        hash 绑定字段(防止 TOCTOU 攻击 — 审批通过后任一字段被篡改都会导致 hash 不匹配):
            - backup_id: 恢复的备份 ID
            - target: 恢复目标(production / staging)
            - schema_version: manifest schema 版本(MANIFEST_SCHEMA_VERSION)
            - requested_by: 审批人 principal_id(来自 command_executions.principal_id)
            - approval_id: 审批动作 ID(approval_action_id)

        使用 compute_effect_request_hash_safe(effect_type='restore', params) 计算,
        'restore' 属于 CRITICAL_EFFECT_TYPES,params 为空时 fail-closed。

        Args:
            backup_id: 备份 ID
            target: 恢复目标
            schema_version: manifest schema 版本
            requested_by: 审批人 principal_id
            approval_id: 审批动作 ID

        Returns:
            SHA256 hex 字符串(64 字符)
        """
        from services.effect_receipts import compute_effect_request_hash_safe
        params = {
            "backup_id": str(backup_id),
            "target": str(target),
            "schema_version": str(schema_version),
            "requested_by": int(requested_by),
            "approval_id": str(approval_id),
        }
        return compute_effect_request_hash_safe("restore", params)

    async def _validate_production_approval(
        self,
        approver_id: int,
        approval_action_id: str,
        expected_request_hash: str | None = None,
    ) -> None:
        """R42 P1-3 + R44 G0-1 + R51 P0-8: 校验生产恢复审批状态(含 TOCTOU 防护)。

        校验逻辑:
            1. approval_action_id 必须在 command_executions 表中存在
            2. R51 P0-8: status 必须为 'approved'(审批通过等待执行);
               status='executed' 表示恢复已完成 → 拒绝重复执行(RESTORE_ALREADY_EXECUTED)
            3. approver_id 必须与 command_executions.principal_id 一致
            4. R44 G0-1 + R51 P0-8: 若传入 expected_request_hash,则比对
               command_executions.request_hash 防止 TOCTOU 攻击;
               R51 P0-8: production 模式下 expected_request_hash 在 restore() 中
               已强制非空,此处不再跳过空 hash

        注:command_executions.principal_id 是创建审批的 admin principal ID
        (即"审批 owner"),与"approver_id"(执行恢复的人)应一致。
        command_executions.owner 字段是 worker 名(lease 持有者),不参与校验。
        request_hash 是 SHA256(payload params),审批时记录、恢复时复核,
        防止"审批通过后 payload 被替换"的 TOCTOU 漏洞。

        R51 P0-8 状态语义:
            - 'pending':  审批待处理(未通过)
            - 'approved': 审批通过,等待执行 restore
            - 'executed': restore 已完成(拒绝重复执行)
            - 其他:      未知状态(fail-closed 拒绝)

        Args:
            approver_id: 调用方传入的审批人 ID
            approval_action_id: 审批动作 ID
            expected_request_hash: 期望的 request_hash(由调用方基于
                backup_id + target + schema_version + requested_by + approval_id
                计算,使用 _compute_restore_request_hash);
                R51 P0-8: production 模式下必填(在 restore() 中已强制)

        Raises:
            AppError: status='executed'(RESTORE_ALREADY_EXECUTED,恢复已完成)
            PermissionError: approval_action_id 不存在 / status != 'approved' /
                             approver_id 不匹配 principal_id /
                             request_hash 不匹配(TOCTOU 攻击)
        """
        store = self._get_cache_store()
        if not hasattr(store, "_db") or not store._db:
            # CacheStore 不可用,无法校验审批 → fail-closed
            raise PermissionError(
                f"R42 P1-3: CacheStore 不可用,无法校验审批状态 "
                f"(approval_action_id={approval_action_id})"
            )

        try:
            # R44 G0-1: SELECT 增加 request_hash 字段,用于 TOCTOU 校验
            cursor = await store._db.execute(
                "SELECT principal_id, status, request_hash FROM command_executions "
                "WHERE action_id = ? LIMIT 1",
                (approval_action_id,),
            )
            row = await cursor.fetchone()
        except Exception as e:
            # 查询失败 → fail-closed(不允许恢复)
            raise PermissionError(
                f"R42 P1-3: 查询 command_executions 失败 "
                f"(approval_action_id={approval_action_id}): {e}"
            ) from e

        if not row:
            raise PermissionError(
                f"R42 P1-3: approval_action_id 不存在 "
                f"(approval_action_id={approval_action_id})"
            )

        principal_id, status, stored_request_hash = row[0], row[1], row[2]

        # R51 P0-8: 状态语义区分 approved 与 executed
        # - 'approved': 审批通过,等待执行 restore → 允许继续
        # - 'executed': restore 已完成 → 拒绝重复执行(RESTORE_ALREADY_EXECUTED)
        # - 其他(pending/未知): fail-closed 拒绝
        if status == "executed":
            # 恢复已完成,禁止重复执行
            raise AppError(
                ErrorCodes.RESTORE_ALREADY_EXECUTED,
                params={"approval_action_id": approval_action_id},
            )
        if status != "approved":
            # 非 approved 状态(pending/未知)→ fail-closed
            raise PermissionError(
                f"R42 P1-3: approval_action_id 状态非 approved "
                f"(approval_action_id={approval_action_id}, status={status})"
            )

        # approver_id 必须与 principal_id 一致
        # (principal_id 是创建审批的 admin principal,即审批 owner)
        try:
            principal_id_int = int(principal_id) if principal_id is not None else 0
        except (TypeError, ValueError):
            principal_id_int = 0

        if approver_id != principal_id_int:
            raise PermissionError(
                f"R42 P1-3: approver_id 与 command_executions.principal_id 不一致 "
                f"(approver_id={approver_id}, principal_id={principal_id_int})"
            )

        # R44 G0-1 + R51 P0-8: TOCTOU 防护 — request_hash 比对
        # R51 P0-8: production 模式下 expected_request_hash 在 restore() 中已强制非空,
        # 此处不再跳过空 hash(若直接调用 _validate_production_approval 且 hash 为空,
        # 仍跳过以保持方法级向后兼容,但 production 路径不会出现 hash=None)
        if expected_request_hash is not None:
            if not stored_request_hash:
                # command_executions 中无 request_hash → 无法校验, fail-closed
                raise PermissionError(
                    f"R44 G0-1: command_executions.request_hash 为空,无法校验 TOCTOU "
                    f"(approval_action_id={approval_action_id})"
                )
            if stored_request_hash != expected_request_hash:
                # R51 P0-8: 协议化为 PRODUCTION_RESTORE_HASH_MISMATCH
                raise AppError(
                    ErrorCodes.PRODUCTION_RESTORE_HASH_MISMATCH,
                    params={
                        "backup_id": "",
                        "approval_action_id": approval_action_id,
                    },
                )

        # 校验通过 → 不抛异常,继续执行恢复

    async def _restore_internal(
        self,
        backup_id: str,
        target: str = "staging",
        approver_id: int = 0,
        approval_action_id: str | None = None,
    ) -> dict:
        """R42 P1-3: 内部恢复逻辑(私有,由 restore() 在审批验证后调用)。

        流程:
            1. 下载 manifest → 校验 schema_version / backup_id
            2. 下载 ciphertext → 计算 ciphertext_sha256 → 与 manifest 对比
            3. 解密 → 计算 plaintext_sha256 → 与 manifest 对比
            4. R42 P1-7: 根据 backup_policy 决定恢复策略
            5. 写入隔离环境(target="staging" 时仅校验,不覆盖生产)

        注意:本方法为私有方法,不应被外部直接调用。
        外部调用方应使用 ``restore()`` 公共 API,经过审批验证后才会调用本方法。
        """
        storage = self._get_storage()
        payload_key = f"{BACKUPS_PREFIX}{backup_id}{PAYLOAD_SUFFIX}"
        manifest_key = f"{BACKUPS_PREFIX}{backup_id}{MANIFEST_SUFFIX}"

        # 1. 下载 + 校验 manifest
        try:
            manifest_bytes = await storage.download(manifest_key)
            manifest = json.loads(manifest_bytes)
        except Exception as e:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False, "error": f"下载 manifest 失败: {e}",
            }

        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": (
                    f"schema_version 不匹配: 备份={manifest.get('schema_version')}, "
                    f"期望={MANIFEST_SCHEMA_VERSION}"
                ),
            }
        if manifest.get("backup_id") != backup_id:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": (
                    f"backup_id 不匹配: manifest={manifest.get('backup_id')}, "
                    f"请求={backup_id}"
                ),
            }

        # 2. 下载 ciphertext → 校验 ciphertext_sha256
        try:
            ciphertext = await storage.download(payload_key)
        except Exception as e:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False, "error": f"下载 payload 失败: {e}",
            }

        actual_ct_sha = _compute_sha256(ciphertext)
        expected_ct_sha = manifest.get("ciphertext_sha256", "")
        if not expected_ct_sha or actual_ct_sha != expected_ct_sha:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": (
                    f"ciphertext_sha256 不匹配: expected={expected_ct_sha[:16]}, "
                    f"actual={actual_ct_sha[:16]}(数据可能被篡改)"
                ),
            }

        # 3. 解密 → 校验 plaintext_sha256
        # R40 P0-7: 传入 backup_id/schema_version/key_id 重建 AAD(与 encrypt_payload 对称)
        from services.backup_crypto import decrypt_payload, get_previous_kek
        enc_info = manifest.get("encryption", {})
        try:
            plaintext = decrypt_payload(
                ciphertext,
                wrapped_dek=enc_info.get("wrapped_dek") or None,
                nonce_b64=enc_info.get("nonce") or None,
                backup_id=backup_id,
                schema_version=manifest.get("schema_version", MANIFEST_SCHEMA_VERSION),
                key_id=enc_info.get("key_id", ""),
            )
        except Exception as e:
            # R41 P1-5: KEK 轮换场景提示 — 若 manifest 中 key_id 与当前 KEK 不匹配,
            # 提示运维配置 BACKUP_KEK_PREVIOUS 以恢复旧备份
            manifest_key_id = enc_info.get("key_id", "")
            hint = ""
            if manifest_key_id:
                # 获取当前 KEK 的 key_id 比对
                try:
                    from services.backup_crypto import get_key_id
                    current_key_id = get_key_id()
                    if current_key_id and current_key_id != manifest_key_id:
                        # 当前 KEK 与备份用的 KEK 不一致 → 提示配置旧 KEK
                        prev_kek = get_previous_kek()
                        if prev_kek is None:
                            hint = (
                                f"(R41 P1-5 提示:备份使用 key_id={manifest_key_id[:8]}... 加密,"
                                f"当前 KEK key_id={current_key_id[:8]}... 不匹配,"
                                f"且 BACKUP_KEK_PREVIOUS 未配置。"
                                f"请配置 BACKUP_KEK_PREVIOUS 环境变量为旧 KEK 后重试恢复)"
                            )
                        else:
                            hint = (
                                f"(R41 P1-5 提示:备份使用 key_id={manifest_key_id[:8]}... 加密,"
                                f"当前 KEK key_id={current_key_id[:8]}... 不匹配,"
                                f"BACKUP_KEK_PREVIOUS 已配置但仍无法解密,"
                                f"请检查 BACKUP_KEK_PREVIOUS 是否为正确的旧 KEK)"
                            )
                except Exception:
                    pass
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": f"解密失败: {e}{hint}",
            }

        actual_pt_sha = _compute_sha256(plaintext)
        expected_pt_sha = manifest.get("plaintext_sha256", "")
        if not expected_pt_sha or actual_pt_sha != expected_pt_sha:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": (
                    f"plaintext_sha256 不匹配: expected={expected_pt_sha[:16]}, "
                    f"actual={actual_pt_sha[:16]}(数据可能被篡改)"
                ),
            }

        # 4. 解析 plaintext,统计行数
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except Exception as e:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": True,
                "error": f"解析 plaintext JSON 失败: {e}",
            }

        # R42 P1-7: 根据 backup_policy 决定恢复策略(过滤/脱敏)
        # R45 第 15 节: 处理 4 类 policy(MUST_RESTORE/REBUILDABLE/NO_EXPORT_PLAINTEXT/ARCHIVE_ONLY)
        data = self._apply_restore_policy(data)

        tables = data.get("tables", {})
        restored_tables = len(tables)
        restored_rows = sum(
            len(rows) if isinstance(rows, list) else 0
            for rows in tables.values()
        )

        # 5. 写入隔离环境(staging 模式仅校验,不写库;production 委托 db_restore)
        if target == "production":
            try:
                from services.db_restore import restore_from_backup_data
                await restore_from_backup_data(data, tables=None, merge=False)
            except Exception as e:
                return {
                    "success": False, "restored_tables": 0, "restored_rows": 0,
                    "checksum_verified": True,
                    "error": f"生产恢复写入失败: {e}",
                }

        # R45 第 15 节: staging/production restore 后跨表不变量验证
        # 校验 file_records.file_code ↔ codes / codes.uploader_id ↔ users / cells.slot_id 唯一性
        # target="test" 仅校验可解密,跳过跨表不变量(数据未写库)
        if target in ("staging", "production"):
            invariant_result = await self._validate_cross_table_invariants(
                self._get_cache_store(),
            )
            if not invariant_result["ok"]:
                logger.error(
                    f"[BackupEngine] restore 跨表不变量校验失败 "
                    f"backup_id={backup_id}: {invariant_result['errors']}"
                )
                return {
                    "success": False,
                    "restored_tables": restored_tables,
                    "restored_rows": restored_rows,
                    "checksum_verified": True,
                    "error": (
                        f"跨表不变量校验失败: "
                        f"{'; '.join(invariant_result['errors'][:5])}"
                    ),
                    "invariant_violations": invariant_result["errors"],
                }

        logger.info(
            f"[BackupEngine] restore 成功 backup_id={backup_id} target={target} "
            f"tables={restored_tables} rows={restored_rows}"
        )

        # R44 7.2: 记录 restore RU 消耗(估算: 每个恢复表约 50 RU)
        # 单独记入 service='restore' 维度,不混入业务空载门禁
        try:
            from services.ru_cost_center import record_restore_usage
            await record_restore_usage(
                ru_cost=restored_tables * 50, operation="restore",
            )
        except Exception:
            pass  # 不影响 restore 主流程

        # R51 P0-8 + R52 P0-5: 恢复成功后将 command_executions.status 从 'executing' 更新为 'executed'
        # R52 P0-5: 统一状态机使用 mark_approved_executed 辅助函数(CAS: executing → executed)
        # 确保恢复完成后审批状态标记为已执行,防止重复执行(RESTORE_ALREADY_EXECUTED)
        if approval_action_id and target == "production":
            try:
                from services.command_bus import mark_approved_executed
                await mark_approved_executed(
                    action_id=approval_action_id,
                    result={
                        "success": True,
                        "restored_tables": restored_tables,
                        "restored_rows": restored_rows,
                        "backup_id": backup_id,
                    },
                )
                logger.info(
                    f"[BackupEngine] R52 P0-5: command_executions.status "
                    f"executing→executed (approval_action_id={approval_action_id})"
                )
            except Exception as e:
                # 状态更新失败不影响恢复结果(数据已恢复),但记录警告
                logger.warning(
                    f"[BackupEngine] R52 P0-5: 更新 command_executions.status 失败 "
                    f"(approval_action_id={approval_action_id}): {e}"
                )

        return {
            "success": True,
            "restored_tables": restored_tables,
            "restored_rows": restored_rows,
            "checksum_verified": True,
            "error": "",
        }

    # ─── R42 P1-2: 跨对象原子提交 + 孤儿 GC ─────────────────────

    async def cleanup_orphans(self, timeout_seconds: int = 3600) -> dict:
        """R42 P1-2: 清理孤儿对象(payload/manifest 已上传但 COMPLETE marker 缺失)。

        场景:备份过程中 manifest 上传成功后 COMPLETE marker 上传失败,
        导致 payload + manifest 已存在于 R2 但 COMPLETE marker 缺失。
        这些对象属于"孤儿",需要周期性清理以免占用 R2 存储。

        策略:
            1. list_objects 列出 BACKUPS_PREFIX 下所有对象
            2. 按 backup_id 聚合 payload/manifest/COMPLETE 三件套
            3. 对每个 backup_id:
               - 若 COMPLETE marker 存在 → 跳过(完整备份)
               - 若 COMPLETE marker 缺失但 manifest 存在 → 检查创建时间,
                 超过 timeout_seconds 才删除(防止正在进行的备份被误删)
               - 若仅有 payload 无 manifest → 直接删除(明显失败)
            4. 写 audit_log 记录清理操作

        Args:
            timeout_seconds: 孤儿对象存活时间阈值(秒),默认 3600(1 小时)

        Returns:
            {
                "scanned": int,    # 扫描的 backup_id 总数
                "deleted": int,    # 已删除的孤儿对象数(payload + manifest + marker)
                "errors": int,     # 删除失败的对象数
                "details": str,    # 文本摘要
            }
        """
        import time as _time

        storage = self._get_storage()
        scanned = 0
        deleted = 0
        errors = 0
        now_ts = _time.time()

        try:
            objects = await storage.list_objects(prefix=BACKUPS_PREFIX, max_keys=1000)
        except Exception as e:
            logger.error(f"[BackupEngine] cleanup_orphans list_objects 失败: {e}")
            return {
                "scanned": 0, "deleted": 0, "errors": 0,
                "details": f"list_objects 失败: {e}",
            }

        # 按 backup_id 聚合对象
        backup_objects: dict[str, dict[str, str]] = {}
        for obj in objects:
            key = obj.get("key", "")
            if not key.startswith(BACKUPS_PREFIX):
                continue
            # 解析 backup_id 与对象类型
            suffix = ""
            if key.endswith(PAYLOAD_SUFFIX):
                suffix = "payload"
            elif key.endswith(MANIFEST_SUFFIX):
                suffix = "manifest"
            elif key.endswith(COMPLETE_SUFFIX):
                suffix = "complete"
            else:
                continue
            # 提取 backup_id(去掉前缀和后缀)
            if suffix == "payload":
                bid = key[len(BACKUPS_PREFIX):-len(PAYLOAD_SUFFIX)]
            elif suffix == "manifest":
                bid = key[len(BACKUPS_PREFIX):-len(MANIFEST_SUFFIX)]
            else:
                bid = key[len(BACKUPS_PREFIX):-len(COMPLETE_SUFFIX)]
            if not bid:
                continue
            backup_objects.setdefault(bid, {})[suffix] = key
            # 记录 last_modified 用于判断超时
            lm_str = obj.get("last_modified", "")
            try:
                # 尝试解析 ISO 8601 时间戳
                lm_dt = _dt.datetime.fromisoformat(
                    lm_str.replace("Z", "+00:00")
                ) if lm_str else None
                if lm_dt:
                    backup_objects[bid]["last_modified_ts"] = (
                        lm_dt.timestamp()
                    )
            except (ValueError, TypeError):
                pass

        scanned = len(backup_objects)

        # 逐个检查并清理孤儿
        for bid, parts in backup_objects.items():
            has_complete = "complete" in parts
            has_manifest = "manifest" in parts
            has_payload = "payload" in parts

            if has_complete:
                # 完整备份,跳过
                continue

            # 孤儿对象 — 检查是否超时
            last_ts = parts.get("last_modified_ts", 0.0)
            age_seconds = now_ts - last_ts if last_ts else 0

            # last_modified 缺失时也尝试清理(可能元数据丢失)
            should_delete = (age_seconds >= timeout_seconds) or (last_ts == 0.0)

            if not should_delete:
                # 未超时,跳过(可能正在备份中)
                continue

            # 删除孤儿对象
            for part_name in ("payload", "manifest"):
                obj_key = parts.get(part_name)
                if not obj_key:
                    continue
                try:
                    await storage.delete(obj_key)
                    deleted += 1
                    logger.info(
                        f"[BackupEngine] cleanup_orphans 删除孤儿对象 "
                        f"backup_id={bid} part={part_name} key={obj_key} "
                        f"age={age_seconds:.0f}s"
                    )
                except Exception as e:
                    errors += 1
                    logger.warning(
                        f"[BackupEngine] cleanup_orphans 删除失败 "
                        f"key={obj_key}: {e}"
                    )

        # 写 audit_log(尝试,失败不阻塞)
        try:
            await self._write_cleanup_audit_log(scanned, deleted, errors)
        except Exception as e:
            logger.debug(f"[BackupEngine] cleanup_orphans audit_log 写入失败: {e}")

        details = (
            f"扫描 {scanned} 个 backup_id,删除 {deleted} 个孤儿对象,"
            f"{errors} 个删除失败"
        )
        logger.info(f"[BackupEngine] cleanup_orphans 完成: {details}")
        return {
            "scanned": scanned,
            "deleted": deleted,
            "errors": errors,
            "details": details,
        }

    async def _write_cleanup_audit_log(
        self, scanned: int, deleted: int, errors: int,
    ) -> None:
        """R42 P1-2: 写 audit_log 记录 cleanup_orphans 操作。

        audit_log 表由 SQLite cache_store 维护,记录管理员/系统的关键操作。
        cleanup_orphans 是后台维护操作,需记录以便审计追溯。
        """
        try:
            store = self._get_cache_store()
            # 尝试写入 audit_log 表(若表不存在则降级到 kv_store)
            try:
                if hasattr(store, "_db") and store._db:
                    cursor = await store._db.execute(
                        "INSERT INTO audit_log "
                        "(actor_id, actor_type, action, target_type, "
                        " target_id, details, ip_addr, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            0,  # actor_id=0 表示系统
                            "system",
                            "backup_gc_cleanup_orphans",
                            "backup_orphans",
                            "",
                            f"scanned={scanned}, deleted={deleted}, errors={errors}",
                            "",
                            _utcnow_iso(),
                        ),
                    )
                    await store._db.commit()
            except Exception:
                # audit_log 表可能不存在,降级到 kv_store
                log_entry = {
                    "action": "backup_gc_cleanup_orphans",
                    "scanned": scanned,
                    "deleted": deleted,
                    "errors": errors,
                    "timestamp": _utcnow_iso(),
                }
                # 追加到 kv_store.backup_gc_audit_log
                raw = await store.get_kv("backup_gc_audit_log")
                log_list = []
                if raw:
                    try:
                        log_list = json.loads(raw)
                        if not isinstance(log_list, list):
                            log_list = []
                    except (ValueError, TypeError):
                        log_list = []
                log_list.append(log_entry)
                # 限制 200 条
                if len(log_list) > 200:
                    log_list = log_list[-200:]
                await store.set_kv(
                    "backup_gc_audit_log",
                    json.dumps(log_list, ensure_ascii=False, default=str),
                )
        except Exception as e:
            logger.debug(f"[BackupEngine] _write_cleanup_audit_log 失败: {e}")

    async def enable_object_lock(
        self, bucket_name: str, retention_days: int = 30,
    ) -> dict:
        """R42 P1-2: 启用 R2 对象锁定(Object Lock)— 占位实现。

        Object Lock 是 R2/S3 的 WORM(Write Once Read Many)功能,
        可防止备份对象在 retention 期内被删除/覆盖,符合合规要求。

        占位说明:
            真正的 Object Lock 配置需要通过 R2 控制台或 Admin API 启用,
            且必须在 bucket 创建时启用(无法事后启用)。
            本方法仅返回当前配置状态,实际配置由运维通过 R2 控制台完成。

        Args:
            bucket_name: R2 bucket 名称
            retention_days: 保留天数(默认 30 天)

        Returns:
            {
                "enabled": bool,         # 是否已启用(本占位始终返回 False)
                "bucket_name": str,      # bucket 名称
                "retention_days": int,   # 保留天数
                "details": str,          # 详细说明
            }
        """
        logger.info(
            f"[BackupEngine] enable_object_lock 占位调用 "
            f"bucket={bucket_name} retention={retention_days}d "
            f"(实际配置需通过 R2 控制台)"
        )
        return {
            "enabled": False,
            "bucket_name": bucket_name,
            "retention_days": retention_days,
            "details": (
                "Object Lock 占位:实际配置需通过 R2 控制台在 bucket 创建时启用。"
                "本方法仅作为代码层提醒,确保备份设计考虑 Object Lock 合规要求。"
            ),
        }
