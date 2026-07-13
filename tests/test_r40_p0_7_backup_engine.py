"""R40 P0-7: BackupEngine 真实 R2 备份 + 恢复可解密测试。

测试覆盖:
- create_backup 生成完整 manifest + payload + complete marker
- list_backups 过滤无 complete marker 的备份
- verify_backup 失败场景(payload 缺失/checksum 不匹配)
- restore 加密备份成功
- restore 篡改 ciphertext 失败
- trigger_backup 失败时不更新 last_backup_at
- get_last_backup_age 无备份返回 None

测试策略:
- Mock R2 storage(避免真实 R2 调用)
- Mock cache_store(避免真实 SQLite)
- 使用真实 backup_crypto(AES-256-GCM)加密,验证可解密
"""
from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# 备份加密可用性检查(决定是否运行加密测试)
try:
    from services.backup_crypto import _CRYPTO_AVAILABLE  # noqa: F401
    _ENCRYPT_AVAILABLE = _CRYPTO_AVAILABLE
except Exception:
    _ENCRYPT_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# 辅助: 构造 mock storage 与 mock cache_store
# ════════════════════════════════════════════════════════════════

class _FakeR2Storage:
    """模拟 R2 storage:用内存字典存储所有上传的对象。

    支持 upload/download/delete/list_objects,并允许人为"破坏"对象
    以测试 checksum 不匹配场景。
    """

    def __init__(self):
        self._objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "") -> str:
        self._objects[key] = bytes(data)
        return key

    async def download(self, key: str) -> bytes:
        if key not in self._objects:
            raise KeyError(f"R2 object not found: {key}")
        return self._objects[key]

    async def delete(self, key: str):
        self._objects.pop(key, None)

    async def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict]:
        result = []
        for key, data in self._objects.items():
            if key.startswith(prefix):
                result.append({
                    "key": key,
                    "size": len(data),
                    "last_modified": "2026-07-13T10:00:00.000Z",
                })
            if len(result) >= max_keys:
                break
        return result

    # 测试辅助方法

    def _corrupt(self, key: str) -> None:
        """人为篡改已上传对象的内容(测试 checksum 不匹配)。"""
        if key in self._objects:
            original = self._objects[key]
            # 翻转最后几个字节,确保 SHA-256 改变
            tampered = original[:-1] + bytes([original[-1] ^ 0xFF]) if original else b"\x00"
            self._objects[key] = tampered

    def _remove(self, key: str) -> None:
        """人为删除已上传对象(测试缺失场景)。"""
        self._objects.pop(key, None)


class _FakeCacheStore:
    """模拟 cache_store:仅提供 get_kv/set_kv 接口。"""

    def __init__(self):
        self._kv: dict[str, str] = {}

    async def get_kv(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set_kv(self, key: str, value: str):
        self._kv[key] = value


def _build_engine_with_kek(monkeypatch, kek_b64: str | None = None):
    """构造一个注入 mock storage/cache_store 的 BackupEngine,并设置 BACKUP_KEK。

    Args:
        monkeypatch: pytest monkeypatch
        kek_b64: KEK base64 字符串,None 时生成新 KEK

    Returns:
        (engine, fake_storage, fake_cache, kek_b64)
    """
    from services.backup_engine import BackupEngine
    from services.backup_crypto import generate_kek

    if kek_b64 is None:
        kek_b64 = generate_kek()
    monkeypatch.setenv("BACKUP_KEK", kek_b64)

    fake_storage = _FakeR2Storage()
    fake_cache = _FakeCacheStore()
    engine = BackupEngine(storage=fake_storage, cache_store=fake_cache)
    return engine, fake_storage, fake_cache, kek_b64


def _patch_backup_all_tables(monkeypatch, tables: dict | None = None):
    """Mock services.db_backup.backup_all_tables 返回固定 backup_data。"""
    if tables is None:
        tables = {
            "users": [{"user_id": 1, "name": "alice"}, {"user_id": 2, "name": "bob"}],
            "file_records": [{"file_code": "ABC123", "status": "active"}],
        }

    async def _fake_backup_all_tables(watermark=None, backup_type="full"):
        return {
            "backup_time": "2026-07-13T10:00:00",
            "tables": tables,
            "_r38_p1_5_metadata": {
                "start_time": "2026-07-13T10:00:00",
                "end_time": "2026-07-13T10:00:01",
                "backup_type": backup_type,
                "watermark": None,
                "prev_watermark": None,
            },
        }

    # patch import path inside BackupEngine.create_backup
    monkeypatch.setattr(
        "services.db_backup.backup_all_tables", _fake_backup_all_tables,
    )
    return tables


# ════════════════════════════════════════════════════════════════
# 1. create_backup 测试
# ════════════════════════════════════════════════════════════════

class TestCreateBackup:
    """R40 P0-7: BackupEngine.create_backup 测试。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_create_backup_generates_full_artifacts(self, monkeypatch):
        """create_backup 应同时生成 manifest + payload + complete marker。"""
        tables = _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")

        backup_id = manifest["backup_id"]
        assert backup_id.startswith("backup_"), f"backup_id 应以 backup_ 开头: {backup_id}"

        # 校验三个 R2 对象都存在
        payload_key = f"backups/{backup_id}.enc"
        manifest_key = f"backups/{backup_id}.manifest.json"
        complete_key = f"backups/{backup_id}.complete"
        assert payload_key in storage._objects, "payload 未上传"
        assert manifest_key in storage._objects, "manifest 未上传"
        assert complete_key in storage._objects, "COMPLETE marker 未上传"

        # 校验 manifest 字段完整
        manifest_data = json.loads(storage._objects[manifest_key])
        required_fields = [
            "schema_version", "backup_id", "created_at",
            "plaintext_sha256", "ciphertext_sha256",
            "plaintext_size_bytes", "ciphertext_size_bytes",
            "encryption", "payload_key",
        ]
        for f in required_fields:
            assert f in manifest_data, f"manifest 缺少字段: {f}"

        # 校验 backup_id 一致性
        assert manifest_data["backup_id"] == backup_id
        # 校验 schema_version
        assert manifest_data["schema_version"] == "r40_p0_7_v1"
        # 校验加密信息
        enc_info = manifest_data["encryption"]
        assert enc_info["encrypted"] is True
        assert enc_info["algorithm"] == "AES-256-GCM"
        assert enc_info["wrapped_dek"]
        assert enc_info["nonce"]
        assert enc_info["key_id"]

        # 校验 checksum 一致性
        payload = storage._objects[payload_key]
        assert manifest_data["ciphertext_sha256"] == hashlib.sha256(payload).hexdigest()

        # 校验 last_backup_at 已更新
        last_at = await cache.get_kv("last_backup_at")
        assert last_at is not None, "last_backup_at 未更新"
        assert last_at == manifest_data["created_at"]

    @pytest.mark.asyncio
    async def test_create_backup_payload_upload_failure_no_last_backup_at(self, monkeypatch):
        """payload 上传失败时,last_backup_at 不应被更新。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 让 storage.upload 第一次(payload)就抛异常
        async def _fail_upload(key, data, content_type=""):
            raise RuntimeError("R2 网络故障")
        storage.upload = _fail_upload

        with pytest.raises(RuntimeError, match="上传 payload 失败"):
            await engine.create_backup(backup_type="full")

        # last_backup_at 不应被更新
        last_at = await cache.get_kv("last_backup_at")
        assert last_at is None, "payload 上传失败时 last_backup_at 不应被更新"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_create_backup_manifest_failure_rolls_back_payload(self, monkeypatch):
        """manifest 上传失败时,已上传的 payload 应被清理。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 包装 upload:manifest 上传时抛异常,其他正常
        original_upload = storage.upload
        call_count = {"n": 0}

        async def _partial_fail_upload(key, data, content_type=""):
            call_count["n"] += 1
            if key.endswith(".manifest.json"):
                raise RuntimeError("manifest 上传失败")
            return await original_upload(key, data, content_type)

        storage.upload = _partial_fail_upload

        with pytest.raises(RuntimeError, match="上传 manifest 失败"):
            await engine.create_backup(backup_type="full")

        # payload 应被清理(回滚)
        # 收集所有 .enc 后缀的 key
        enc_keys = [k for k in storage._objects if k.endswith(".enc")]
        assert not enc_keys, f"manifest 失败后 payload 应被清理,但仍存在: {enc_keys}"

        # last_backup_at 不应被更新
        last_at = await cache.get_kv("last_backup_at")
        assert last_at is None

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_create_backup_complete_marker_failure_rolls_back(self, monkeypatch):
        """COMPLETE marker 上传失败时,payload 与 manifest 都应被清理。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        original_upload = storage.upload

        async def _fail_complete_upload(key, data, content_type=""):
            if key.endswith(".complete"):
                raise RuntimeError("COMPLETE marker 上传失败")
            return await original_upload(key, data, content_type)

        storage.upload = _fail_complete_upload

        with pytest.raises(RuntimeError, match="上传 COMPLETE marker 失败"):
            await engine.create_backup(backup_type="full")

        # payload 与 manifest 都应被清理
        enc_keys = [k for k in storage._objects if k.endswith(".enc")]
        manifest_keys = [k for k in storage._objects if k.endswith(".manifest.json")]
        complete_keys = [k for k in storage._objects if k.endswith(".complete")]
        assert not enc_keys
        assert not manifest_keys
        assert not complete_keys

        last_at = await cache.get_kv("last_backup_at")
        assert last_at is None


# ════════════════════════════════════════════════════════════════
# 2. list_backups 测试
# ════════════════════════════════════════════════════════════════

class TestListBackups:
    """R40 P0-7: BackupEngine.list_backups 过滤无 complete marker 的备份。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_list_backups_filters_incomplete(self, monkeypatch):
        """list_backups 应过滤掉无 complete marker 的备份。"""
        # 创建两个完整备份 + 一个不完整(缺 complete marker)
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 创建两个完整备份
        manifest1 = await engine.create_backup(backup_type="full")
        manifest2 = await engine.create_backup(backup_type="full")

        # 模拟一个不完整备份:仅上传 payload + manifest,无 complete marker
        fake_backup_id = "backup_20260101_000000_incomplet"
        storage._objects[f"backups/{fake_backup_id}.enc"] = b"fake_payload"
        storage._objects[f"backups/{fake_backup_id}.manifest.json"] = json.dumps({
            "schema_version": "r40_p0_7_v1",
            "backup_id": fake_backup_id,
            "created_at": "2026-01-01T00:00:00",
            "ciphertext_sha256": "fake",
            "encryption": {"encrypted": True, "algorithm": "AES-256-GCM"},
        }).encode("utf-8")
        # 故意不上传 .complete marker

        backups = await engine.list_backups()

        backup_ids = [b["backup_id"] for b in backups]
        # 应包含两个完整备份,不包含 incomplete
        assert manifest1["backup_id"] in backup_ids
        assert manifest2["backup_id"] in backup_ids
        assert fake_backup_id not in backup_ids, \
            "list_backups 不应返回无 complete marker 的备份"

    @pytest.mark.asyncio
    async def test_list_backups_empty(self, monkeypatch):
        """R2 无备份时返回空列表。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)
        backups = await engine.list_backups()
        assert backups == []


# ════════════════════════════════════════════════════════════════
# 3. verify_backup 测试
# ════════════════════════════════════════════════════════════════

class TestVerifyBackup:
    """R40 P0-7: BackupEngine.verify_backup 失败场景测试。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_verify_backup_success(self, monkeypatch):
        """正常备份应通过 verify_backup。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        result = await engine.verify_backup(backup_id)
        assert result["valid"] is True
        assert result["manifest_ok"] is True
        assert result["complete_ok"] is True
        assert result["payload_exists"] is True
        assert result["error"] == ""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_verify_backup_payload_missing(self, monkeypatch):
        """payload 缺失时 verify_backup 应失败。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 删除 payload
        storage._remove(f"backups/{backup_id}.enc")

        result = await engine.verify_backup(backup_id)
        assert result["valid"] is False
        assert result["payload_exists"] is False
        assert "payload 缺失" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_verify_backup_complete_marker_missing(self, monkeypatch):
        """complete marker 缺失时 verify_backup 应失败。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 删除 complete marker
        storage._remove(f"backups/{backup_id}.complete")

        result = await engine.verify_backup(backup_id)
        assert result["valid"] is False
        assert result["complete_ok"] is False
        assert "complete marker 缺失" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_verify_backup_manifest_corrupted(self, monkeypatch):
        """manifest 损坏(字段不完整)时 verify_backup 应失败。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 用不完整 manifest 覆盖(缺 ciphertext_sha256)
        broken_manifest = {
            "schema_version": "r40_p0_7_v1",
            "backup_id": backup_id,
            "created_at": "2026-07-13T10:00:00",
            # 缺 plaintext_sha256 / ciphertext_sha256 / encryption
        }
        storage._objects[f"backups/{backup_id}.manifest.json"] = json.dumps(
            broken_manifest,
        ).encode("utf-8")

        result = await engine.verify_backup(backup_id)
        assert result["valid"] is False
        assert result["manifest_ok"] is False
        assert "缺少字段" in result["error"]


# ════════════════════════════════════════════════════════════════
# 4. restore 测试
# ════════════════════════════════════════════════════════════════

class TestRestore:
    """R40 P0-7: BackupEngine.restore 加密备份恢复测试。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_restore_encrypted_backup_success(self, monkeypatch):
        """加密备份可成功解密恢复。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # staging 模式恢复(不写库,仅校验可解密)
        result = await engine.restore(backup_id, target="staging", approver_id=0)

        assert result["success"] is True
        assert result["restored_tables"] == 2  # users + file_records
        assert result["restored_rows"] == 3   # 2 + 1
        assert result["checksum_verified"] is True
        assert result["error"] == ""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_restore_tampered_ciphertext_fails(self, monkeypatch):
        """篡改 ciphertext 后 restore 应失败(ciphertext_sha256 不匹配)。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 篡改 payload 内容(翻转字节)
        storage._corrupt(f"backups/{backup_id}.enc")

        result = await engine.restore(backup_id, target="staging", approver_id=0)
        assert result["success"] is False
        assert result["checksum_verified"] is False
        assert "ciphertext_sha256 不匹配" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_restore_production_requires_approver(self, monkeypatch):
        """生产恢复必须 approver_id 非零。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # production 模式 + approver_id=0 → 应失败
        result = await engine.restore(backup_id, target="production", approver_id=0)
        assert result["success"] is False
        assert "approver_id 非零" in result["error"]
        assert "需通过审批" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_restore_missing_manifest_fails(self, monkeypatch):
        """manifest 缺失时 restore 应失败。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 删除 manifest
        storage._remove(f"backups/{backup_id}.manifest.json")

        result = await engine.restore(backup_id, target="staging", approver_id=0)
        assert result["success"] is False
        assert "下载 manifest 失败" in result["error"]

    @pytest.mark.asyncio
    async def test_restore_empty_backup_id(self, monkeypatch):
        """空 backup_id 应立即返回失败。"""
        engine, _, _, _ = _build_engine_with_kek(monkeypatch)
        result = await engine.restore("", target="staging", approver_id=0)
        assert result["success"] is False
        assert result["error"] == "backup_id 为空"


# ════════════════════════════════════════════════════════════════
# 5. trigger_backup 集成测试(通过 disaster_recovery 入口)
# ════════════════════════════════════════════════════════════════

class TestTriggerBackupIntegration:
    """R40 P0-7: disaster_recovery.trigger_backup 失败时不更新 last_backup_at。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    async def test_trigger_backup_success_updates_last_backup_at(self, monkeypatch):
        """成功备份后 last_backup_at 应被更新。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # patch BackupEngine 构造函数以返回注入 storage/cache 的实例
        monkeypatch.setattr(
            "services.backup_engine.BackupEngine",
            lambda *args, **kwargs: engine,
        )
        # patch get_cache_store 返回我们的 fake cache
        # 注意:disaster_recovery 模块在 import 时已绑定 get_cache_store 名字,
        # 因此必须 patch services.disaster_recovery.get_cache_store 才能生效
        monkeypatch.setattr(
            "services.disaster_recovery.get_cache_store",
            lambda: cache,
        )

        from services import disaster_recovery
        backup_id = await disaster_recovery.trigger_backup()
        assert backup_id.startswith("backup_")

        # last_backup_at 应已更新
        last_at = await cache.get_kv("last_backup_at")
        assert last_at is not None

    @pytest.mark.asyncio
    async def test_trigger_backup_failure_does_not_update_last_backup_at(self, monkeypatch):
        """trigger_backup 失败时 last_backup_at 不应被更新。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 让 storage.upload 总是失败
        async def _fail_upload(key, data, content_type=""):
            raise RuntimeError("R2 不可达")
        storage.upload = _fail_upload

        # patch BackupEngine 构造函数返回注入实例
        monkeypatch.setattr(
            "services.backup_engine.BackupEngine",
            lambda *args, **kwargs: engine,
        )
        monkeypatch.setattr(
            "services.disaster_recovery.get_cache_store",
            lambda: cache,
        )

        from services import disaster_recovery
        backup_id = await disaster_recovery.trigger_backup()
        assert backup_id == "", "trigger_backup 失败应返回空字符串"

        # last_backup_at 不应被更新
        last_at = await cache.get_kv("last_backup_at")
        assert last_at is None, "trigger_backup 失败时 last_backup_at 不应被更新"


# ════════════════════════════════════════════════════════════════
# 6. get_last_backup_age 测试
# ════════════════════════════════════════════════════════════════

class TestGetLastBackupAge:
    """R40 P0-7: get_last_backup_age 无备份返回 None。"""

    @pytest.mark.asyncio
    async def test_no_backup_returns_none(self, monkeypatch):
        """kv_store 无 last_backup_at 时返回 None。"""
        cache = _FakeCacheStore()
        # 注意:disaster_recovery 模块在 import 时已绑定 get_cache_store 名字,
        # 必须 patch services.disaster_recovery.get_cache_store
        monkeypatch.setattr(
            "services.disaster_recovery.get_cache_store",
            lambda: cache,
        )

        from services import disaster_recovery
        age = await disaster_recovery.get_last_backup_age()
        assert age is None, "无备份时应返回 None"

    @pytest.mark.asyncio
    async def test_with_backup_returns_int_seconds(self, monkeypatch):
        """kv_store 有 last_backup_at 时返回秒数(int)。"""
        import datetime as _dt
        cache = _FakeCacheStore()
        # 设置 1 小时前的备份时间
        one_hour_ago = (_dt.datetime.now() - _dt.timedelta(hours=1)).isoformat()
        await cache.set_kv("last_backup_at", one_hour_ago)

        monkeypatch.setattr(
            "services.disaster_recovery.get_cache_store",
            lambda: cache,
        )

        from services import disaster_recovery
        age = await disaster_recovery.get_last_backup_age()
        assert age is not None
        assert isinstance(age, int)
        # 应该大约 3600 秒(允许 ±120 秒误差)
        assert 3480 <= age <= 3720, f"age 不在合理范围: {age}"

    @pytest.mark.asyncio
    async def test_get_rpo_rpo_compliant_false_when_no_backup(self, monkeypatch):
        """无备份时 get_rpo_rto 的 rpo_compliant 应为 False。"""
        cache = _FakeCacheStore()
        monkeypatch.setattr(
            "services.disaster_recovery.get_cache_store",
            lambda: cache,
        )

        from services import disaster_recovery
        result = await disaster_recovery.get_rpo_rto()
        assert result["last_backup_age"] is None
        assert result["rpo_compliant"] is False, \
            "无备份时 rpo_compliant 必须为 False(避免 RPO 假合规)"
