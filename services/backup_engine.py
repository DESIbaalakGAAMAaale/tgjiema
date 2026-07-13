"""R40 P0-7: 统一备份引擎 — 真实 R2 上传 + 可解密恢复。

职责:
    解决原 ``disaster_recovery.trigger_backup`` 只采集数据不上传 R2、
    原文 ``restore`` 直接 ``json.loads(ciphertext)`` 导致无法解密的问题。

    提供统一 BackupEngine 类:
    1. ``create_backup`` — 采集 → 加密 → 上传 payload/manifest/COMPLETE marker → 校验 → 更新 last_backup_at
    2. ``list_backups`` — 仅返回 .complete marker 存在的备份(防部分上传被当作成功)
    3. ``verify_backup`` — HEAD 三个对象 + 下载 manifest 校验(不下载 payload,节省带宽)
    4. ``restore`` — 下载 manifest → 校验 ciphertext_sha256 → 解密 → 校验 plaintext_sha256 → 写隔离环境

设计要点:
    - COMPLETE marker 三阶段提交:payload → manifest → COMPLETE,任一缺失视为备份失败
    - manifest 包含 backup_id/created_at/双 checksum(plaintext+ciphertext)/encryption/size
    - 生产恢复必须审批(approver_id 非零),staging 恢复可免审批
    - last_backup_at 仅在 COMPLETE marker 上传成功后更新(避免 RPO 假合规)
    - 失败时不更新 last_backup_at,记录失败原因到 backup_history
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets as _secrets
from typing import Any

from loguru import logger


# ─── R2 对象 key 命名 ─────────────────────────────────────────
BACKUPS_PREFIX = "backups/"
PAYLOAD_SUFFIX = ".enc"
MANIFEST_SUFFIX = ".manifest.json"
COMPLETE_SUFFIX = ".complete"

# ─── manifest schema 版本 ──────────────────────────────────────
MANIFEST_SCHEMA_VERSION = "r40_p0_7_v1"


def _utcnow_iso() -> str:
    """当前 UTC 时间 ISO 字符串。"""
    return _dt.datetime.utcnow().isoformat()


def _compute_sha256(content: bytes) -> str:
    """计算 SHA-256 校验和。"""
    return hashlib.sha256(content).hexdigest()


class BackupEngine:
    """R40 P0-7: 统一备份引擎。

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
            2. 计算 plaintext_sha256
            3. 调用 ``backup_crypto.encrypt_payload`` 加密,得到 ciphertext + wrapped_dek + nonce + key_id
            4. 计算 ciphertext_sha256
            5. 生成 backup_id(含时间戳 + 短随机后缀)
            6. 上传 payload(.enc) → manifest(.manifest.json) → COMPLETE marker(.complete)
            7. HEAD 三个对象验证全部存在
            8. 全部成功后更新 last_backup_at(写入 kv_config/kv_store)
            9. 返回完整 manifest(包含 backup_id 与所有元信息)

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
            name: {"row_count": len(rows)} for name, rows in tables.items()
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
            "total_rows": sum(len(rows) for rows in tables.values()),
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

        logger.info(
            f"[BackupEngine] 备份成功 backup_id={backup_id} "
            f"payload={len(ciphertext)}B manifest={len(manifest_bytes)}B "
            f"encrypted={enc_result['encrypted']} tables={len(tables)}"
        )
        return manifest

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
        approver_id: int = 0,
    ) -> dict:
        """从 R2 备份恢复数据(可解密)。

        流程:
            1. 下载 manifest → 校验 schema_version / backup_id
            2. 下载 ciphertext → 计算 ciphertext_sha256 → 与 manifest 对比
            3. 解密 → 计算 plaintext_sha256 → 与 manifest 对比
            4. 写入隔离环境(target="staging" 时仅校验,不覆盖生产)
            5. 生产恢复必须 approver_id 非零(由 CommandBus 审批门禁保证)

        Args:
            backup_id: 备份 ID
            target: "staging" 仅校验可解密;"production" 写入生产(需 approver_id != 0)
            approver_id: 审批人 ID(生产恢复时必填)

        Returns:
            {success, restored_tables, restored_rows, checksum_verified, error}
        """
        if not backup_id:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False, "error": "backup_id 为空",
            }

        # 生产恢复必须审批
        if target == "production" and not approver_id:
            return {
                "success": False, "restored_tables": 0, "restored_rows": 0,
                "checksum_verified": False,
                "error": "生产恢复必须 approver_id 非零(需通过审批)",
            }

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

        tables = data.get("tables", {})
        restored_tables = len(tables)
        restored_rows = sum(len(rows) for rows in tables.values())

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

        logger.info(
            f"[BackupEngine] restore 成功 backup_id={backup_id} target={target} "
            f"tables={restored_tables} rows={restored_rows}"
        )
        return {
            "success": True,
            "restored_tables": restored_tables,
            "restored_rows": restored_rows,
            "checksum_verified": True,
            "error": "",
        }
