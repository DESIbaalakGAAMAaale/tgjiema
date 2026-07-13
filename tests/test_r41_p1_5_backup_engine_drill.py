"""R41 P1-5: BackupEngine 真实完整性与恢复演练。

测试覆盖 8 个场景(使用 mock S3 client 模拟完整流程):
  1. 正常上传:create_backup → verify_backup → restore(staging) 全流程成功
  2. 上传中断:manifest 上传失败时 payload 应回滚(不残留孤儿对象)
  3. COMPLETE 缺失:list_backups 必须过滤掉无 complete marker 的备份
  4. 错误 KEK:用 KEK_A 加密,改用 KEK_B 恢复(无 BACKUP_KEK_PREVIOUS)→
     失败且错误消息含 R41 P1-5 提示(建议配置 BACKUP_KEK_PREVIOUS)
  5. 旧 KEK:用 KEK_A 加密,改用 KEK_B + BACKUP_KEK_PREVIOUS=KEK_A 恢复 →
     双 key 窗口解密成功
  6. ciphertext 篡改:翻转 payload 字节 → ciphertext_sha256 不匹配
  7. plaintext 篡改:篡改 manifest.plaintext_sha256 → 解密后明文 checksum 不匹配
  8. staging restore:target="staging" 不写入数据库(仅校验可解密)

测试策略:
  - 复用 R40 P0-7 测试的 mock 模式(_FakeR2Storage / _FakeCacheStore)
  - 使用真实 backup_crypto(AES-256-GCM)加密,验证可解密
  - 不依赖真实 R2 / CRDB / SQLite(全 mock)
"""
from __future__ import annotations

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
# 辅助: mock storage / cache_store / backup_all_tables
# (与 test_r40_p0_7_backup_engine.py 相同的模式,保持测试独立)
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

    def _corrupt(self, key: str) -> None:
        """人为篡改已上传对象的内容(测试 checksum 不匹配)。"""
        if key in self._objects:
            original = self._objects[key]
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
    """构造一个注入 mock storage/cache_store 的 BackupEngine,并设置 BACKUP_KEK。"""
    from services.backup_engine import BackupEngine
    from services.backup_crypto import generate_kek

    if kek_b64 is None:
        kek_b64 = generate_kek()
    monkeypatch.setenv("BACKUP_KEK", kek_b64)
    # 清除 PREVIOUS 以保证干净环境
    monkeypatch.delenv("BACKUP_KEK_PREVIOUS", raising=False)
    monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)

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

    monkeypatch.setattr(
        "services.db_backup.backup_all_tables", _fake_backup_all_tables,
    )
    return tables


# ════════════════════════════════════════════════════════════════
# 场景 1: 正常上传完整流程(create → verify → restore)
# ════════════════════════════════════════════════════════════════


class TestScenario1NormalUploadCompleteCycle:
    """场景 1: 正常上传 → verify_backup → restore(staging) 全流程。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_normal_upload_verify_restore_succeeds(self, monkeypatch):
        """create_backup → verify_backup → restore(staging) 全部成功。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 1. 创建备份
        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]
        assert backup_id.startswith("backup_")

        # 2. verify_backup 应通过
        verify_result = await engine.verify_backup(backup_id)
        assert verify_result["valid"] is True
        assert verify_result["manifest_ok"] is True
        assert verify_result["complete_ok"] is True
        assert verify_result["payload_exists"] is True

        # 3. restore(staging) 应成功(不写库,仅校验可解密)
        restore_result = await engine.restore(backup_id, target="staging", approver_id=0)
        assert restore_result["success"] is True
        assert restore_result["restored_tables"] == 2  # users + file_records
        assert restore_result["restored_rows"] == 3   # 2 + 1
        assert restore_result["checksum_verified"] is True


# ════════════════════════════════════════════════════════════════
# 场景 2: 上传中断(manifest 失败时 payload 回滚)
# ════════════════════════════════════════════════════════════════


class TestScenario2UploadInterruptedRollback:
    """场景 2: manifest 上传失败时 payload 应被清理(不残留孤儿对象)。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_manifest_failure_rolls_back_payload(self, monkeypatch):
        """manifest 上传失败 → payload 应被清理,last_backup_at 不更新。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        original_upload = storage.upload

        async def _fail_on_manifest(key, data, content_type=""):
            if key.endswith(".manifest.json"):
                raise RuntimeError("模拟 R2 网络中断")
            return await original_upload(key, data, content_type)

        storage.upload = _fail_on_manifest

        with pytest.raises(RuntimeError, match="上传 manifest 失败"):
            await engine.create_backup(backup_type="full")

        # payload 不应残留
        enc_keys = [k for k in storage._objects if k.endswith(".enc")]
        assert not enc_keys, f"manifest 失败后 payload 应被清理,但仍存在: {enc_keys}"

        # last_backup_at 不应更新
        last_at = await cache.get_kv("last_backup_at")
        assert last_at is None


# ════════════════════════════════════════════════════════════════
# 场景 3: COMPLETE marker 缺失 → list_backups 过滤
# ════════════════════════════════════════════════════════════════


class TestScenario3CompleteMarkerMissingFiltered:
    """场景 3: 缺少 COMPLETE marker 的备份不应出现在 list_backups。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_list_backups_filters_incomplete(self, monkeypatch):
        """list_backups 必须过滤掉无 complete marker 的备份。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 创建一个完整备份
        manifest = await engine.create_backup(backup_type="full")
        complete_backup_id = manifest["backup_id"]

        # 模拟一个不完整备份(仅 payload + manifest,无 complete marker)
        incomplete_id = "backup_20260101_000000_incomplet"
        storage._objects[f"backups/{incomplete_id}.enc"] = b"fake_payload"
        storage._objects[f"backups/{incomplete_id}.manifest.json"] = json.dumps({
            "schema_version": "r40_p0_7_v1",
            "backup_id": incomplete_id,
            "created_at": "2026-01-01T00:00:00",
            "ciphertext_sha256": "fake",
            "encryption": {"encrypted": True, "algorithm": "AES-256-GCM"},
        }).encode("utf-8")
        # 故意不上传 .complete marker

        backups = await engine.list_backups()

        backup_ids = [b["backup_id"] for b in backups]
        assert complete_backup_id in backup_ids, "完整备份应出现在列表中"
        assert incomplete_id not in backup_ids, \
            "无 complete marker 的备份不应出现在 list_backups"


# ════════════════════════════════════════════════════════════════
# 场景 4: 错误 KEK(无 BACKUP_KEK_PREVIOUS)→ 失败 + R41 P1-5 提示
# ════════════════════════════════════════════════════════════════


class TestScenario4WrongKekShowsHint:
    """场景 4: KEK 轮换后,旧备份无法用新 KEK 解密 → 错误消息含 R41 P1-5 提示。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_wrong_kek_shows_r41_p1_5_hint(self, monkeypatch):
        """KEK_A 加密 → 切换到 KEK_B(无 PREVIOUS)→ 失败且提示配置 BACKUP_KEK_PREVIOUS。"""
        from services.backup_crypto import generate_kek

        # 1. 用 KEK_A 创建备份
        kek_a = generate_kek()
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch, kek_b64=kek_a)
        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]
        # 记录备份用的 key_id
        manifest_key_id = manifest["encryption"]["key_id"]
        assert manifest_key_id, "manifest 应记录 key_id"

        # 2. 切换到 KEK_B(模拟密钥轮换),不配置 PREVIOUS
        kek_b = generate_kek()
        monkeypatch.setenv("BACKUP_KEK", kek_b)
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS", raising=False)
        monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)

        # 3. restore 应失败
        result = await engine.restore(backup_id, target="staging", approver_id=0)

        assert result["success"] is False, "用错误 KEK 恢复应失败"
        assert result["checksum_verified"] is False
        # 4. 错误消息应包含 R41 P1-5 提示(建议配置 BACKUP_KEK_PREVIOUS)
        assert "R41 P1-5" in result["error"], \
            f"错误消息应含 R41 P1-5 提示,实际: {result['error']}"
        assert "BACKUP_KEK_PREVIOUS" in result["error"], \
            f"错误消息应提及 BACKUP_KEK_PREVIOUS,实际: {result['error']}"
        assert "未配置" in result["error"], \
            f"错误消息应说明 PREVIOUS 未配置,实际: {result['error']}"


# ════════════════════════════════════════════════════════════════
# 场景 5: 旧 KEK(配置 BACKUP_KEK_PREVIOUS)→ 双 key 窗口解密成功
# ════════════════════════════════════════════════════════════════


class TestScenario5OldKekViaPreviousSucceeds:
    """场景 5: KEK_A 加密 → 切换到 KEK_B + PREVIOUS=KEK_A → 双 key 解密成功。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_with_previous_kek_succeeds(self, monkeypatch):
        """配置 BACKUP_KEK_PREVIOUS=旧 KEK 后,旧备份应能成功解密恢复。"""
        from services.backup_crypto import generate_kek

        # 1. 用 KEK_A 创建备份
        kek_a = generate_kek()
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch, kek_b64=kek_a)
        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 2. 切换到 KEK_B + 配置 PREVIOUS=KEK_A(双 key 窗口)
        kek_b = generate_kek()
        monkeypatch.setenv("BACKUP_KEK", kek_b)
        monkeypatch.setenv("BACKUP_KEK_PREVIOUS", kek_a)

        # 3. restore 应成功(双 key 窗口:当前 KEK 失败 → 尝试 PREVIOUS)
        result = await engine.restore(backup_id, target="staging", approver_id=0)

        assert result["success"] is True, \
            f"配置 BACKUP_KEK_PREVIOUS 后应能解密旧备份,实际: {result['error']}"
        assert result["restored_tables"] == 2
        assert result["restored_rows"] == 3
        assert result["checksum_verified"] is True


# ════════════════════════════════════════════════════════════════
# 场景 6: ciphertext 篡改 → ciphertext_sha256 不匹配
# ════════════════════════════════════════════════════════════════


class TestScenario6CiphertextTamperedFails:
    """场景 6: 篡改 ciphertext(payload)后 restore 应在 ciphertext_sha256 校验失败。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_tampered_ciphertext_fails_checksum(self, monkeypatch):
        """翻转 payload 字节 → ciphertext_sha256 不匹配 → restore 失败。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 篡改 payload 内容
        storage._corrupt(f"backups/{backup_id}.enc")

        result = await engine.restore(backup_id, target="staging", approver_id=0)

        assert result["success"] is False
        assert result["checksum_verified"] is False
        assert "ciphertext_sha256 不匹配" in result["error"], \
            f"应报告 ciphertext_sha256 不匹配,实际: {result['error']}"
        assert "篡改" in result["error"], \
            f"应提示数据可能被篡改,实际: {result['error']}"


# ════════════════════════════════════════════════════════════════
# 场景 7: plaintext 篡改(篡改 manifest.plaintext_sha256)
# ════════════════════════════════════════════════════════════════


class TestScenario7PlaintextChecksumTamperedFails:
    """场景 7: 篡改 manifest 中的 plaintext_sha256 → 解密后明文 checksum 不匹配。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_tampered_plaintext_checksum_fails(self, monkeypatch):
        """修改 manifest.plaintext_sha256 → 解密成功但 plaintext_sha256 校验失败。"""
        import hashlib

        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 篡改 manifest 中的 plaintext_sha256(改为伪造值)
        manifest_key = f"backups/{backup_id}.manifest.json"
        manifest_data = json.loads(storage._objects[manifest_key])
        # 用一个明显错误的 sha256 值
        manifest_data["plaintext_sha256"] = "a" * 64  # 伪造的 sha256
        storage._objects[manifest_key] = json.dumps(manifest_data).encode("utf-8")

        result = await engine.restore(backup_id, target="staging", approver_id=0)

        assert result["success"] is False
        # checksum_verified=False,因为 plaintext_sha256 不匹配
        # 注意:ciphertext_sha256 仍然匹配(因为 ciphertext 未变),
        # 解密也会成功(因为 KEK + AAD 都对),但 plaintext_sha256 校验失败
        assert "plaintext_sha256 不匹配" in result["error"], \
            f"应报告 plaintext_sha256 不匹配,实际: {result['error']}"


# ════════════════════════════════════════════════════════════════
# 场景 8: staging restore 不写入数据库
# ════════════════════════════════════════════════════════════════


class TestScenario8StagingRestoreNoDbWrite:
    """场景 8: target="staging" 时不应调用 db_restore(仅校验可解密)。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_staging_restore_does_not_call_db_restore(self, monkeypatch):
        """staging restore 不应调用 services.db_restore.restore_from_backup_data。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 监视 db_restore.restore_from_backup_data 是否被调用
        call_count = {"n": 0}

        async def _spy_restore_from_backup_data(*args, **kwargs):
            call_count["n"] += 1
            return {"restored_tables": 0, "restored_rows": 0}

        monkeypatch.setattr(
            "services.db_restore.restore_from_backup_data",
            _spy_restore_from_backup_data,
        )

        # staging 模式恢复
        result = await engine.restore(backup_id, target="staging", approver_id=0)

        assert result["success"] is True
        # staging 模式不应调用 db_restore
        assert call_count["n"] == 0, \
            "staging restore 不应调用 db_restore.restore_from_backup_data"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_calls_db_restore_with_approver(self, monkeypatch):
        """production 模式 + approver_id 非零 + approval_action_id → 应调用 db_restore。

        R42 P1-3: restore(target="production") 必须提供 approval_action_id,
        否则抛 ValueError。本用例提供合法 approval_action_id 并 mock 审批校验通过,
        验证 production restore 仍能正确委托 db_restore。
        """
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 监视 db_restore
        call_count = {"n": 0}

        async def _spy_restore_from_backup_data(*args, **kwargs):
            call_count["n"] += 1
            return {"restored_tables": 2, "restored_rows": 3}

        monkeypatch.setattr(
            "services.db_restore.restore_from_backup_data",
            _spy_restore_from_backup_data,
        )

        # R42 P1-3: mock _validate_production_approval 通过(不抛异常)
        async def _noop_validate(approver_id, approval_action_id):
            return None
        monkeypatch.setattr(engine, "_validate_production_approval", _noop_validate)

        # R42 P1-3: production restore 必须提供 approval_action_id
        result = await engine.restore(
            backup_id, target="production", approver_id=999,
            approval_action_id="approval_test_id",
        )

        assert result["success"] is True
        assert call_count["n"] == 1, \
            "production restore 应调用 db_restore.restore_from_backup_data 一次"
