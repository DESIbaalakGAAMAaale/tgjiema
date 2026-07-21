"""R51 P0-8: Production Restore Hash 强制测试。

测试覆盖 6 大场景:
1. production restore 无 expected_request_hash → raise AppError(PRODUCTION_RESTORE_HASH_REQUIRED)
2. production restore hash 不匹配 → raise AppError(PRODUCTION_RESTORE_HASH_MISMATCH)
3. production restore hash 匹配 → 执行成功
4. command_executions status='executed' → raise AppError(RESTORE_ALREADY_EXECUTED)
5. restore 成功后 status 更新为 'executed'
6. 非 production 模式 → hash 可选(向后兼容)

测试策略:
- Mock R2 storage(避免真实 R2 调用)
- 使用真实 SQLite 临时数据库(校验 command_executions 表查询与更新)
- 使用真实 backup_crypto(AES-256-GCM)加密
- 使用 BackupEngine._compute_restore_request_hash 计算 hash
"""
from __future__ import annotations

import inspect
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from services.error_codes import AppError, ErrorCodes

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore

# 备份加密可用性检查
try:
    from services.backup_crypto import _CRYPTO_AVAILABLE  # noqa: F401
    _ENCRYPT_AVAILABLE = _CRYPTO_AVAILABLE
except Exception:
    _ENCRYPT_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# 辅助: mock R2 storage
# ════════════════════════════════════════════════════════════════

class _FakeR2Storage:
    """模拟 R2 storage:用内存字典存储所有上传的对象。"""

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
                    "last_modified": "2026-07-14T10:00:00.000Z",
                })
            if len(result) >= max_keys:
                break
        return result


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(集成 mock storage 与 KEK)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store_with_engine(monkeypatch):
    """创建一个临时 SQLite + 注入 mock storage 与 KEK 的 BackupEngine。

    Yields:
        (store, engine, fake_storage, kek_b64)
    """
    from services.backup_engine import BackupEngine
    from services.backup_crypto import generate_kek

    tmpdir = tempfile.mkdtemp(prefix="r51_p0_8_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path

    kek_b64 = generate_kek()
    monkeypatch.setenv("BACKUP_KEK", kek_b64)

    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        fake_storage = _FakeR2Storage()
        engine = BackupEngine(storage=fake_storage, cache_store=s)
        yield s, engine, fake_storage, kek_b64
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _patch_backup_all_tables(monkeypatch, tables: dict | None = None):
    """Mock services.db_backup.backup_all_tables 返回固定 backup_data。"""
    if tables is None:
        tables = {
            "users": [{"user_id": 1, "name": "alice"}],
            "file_records": [{"file_code": "ABC123", "status": "active"}],
        }

    async def _fake_backup_all_tables(watermark=None, backup_type="full"):
        return {
            "backup_time": "2026-07-14T10:00:00",
            "tables": tables,
            "_r38_p1_5_metadata": {
                "start_time": "2026-07-14T10:00:00",
                "end_time": "2026-07-14T10:00:01",
                "backup_type": backup_type,
                "watermark": None,
                "prev_watermark": None,
            },
        }

    monkeypatch.setattr(
        "services.db_backup.backup_all_tables", _fake_backup_all_tables,
    )
    return tables


async def _seed_command_executions(
    store,
    action_id: str,
    principal_id: int,
    status: str = "approved",
    request_hash: str = "fake_hash",
) -> None:
    """向 command_executions 表插入一条记录(用于 _validate_production_approval 校验)。

    R51 P0-8: 默认 status='approved'(审批通过等待执行),
    而非旧版 'executed'(恢复已完成)。
    """
    now = "2026-07-14T10:00:00"
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        " request_hash, result, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?)",
        (action_id, "restore_backup", principal_id, status,
         request_hash, now, now),
    )
    await store._db.commit()


async def _make_backup(engine) -> str:
    """通过 BackupEngine.create_backup 创建一个备份,返回 backup_id。"""
    manifest = await engine.create_backup(backup_type="full")
    return manifest["backup_id"]


def _compute_hash(engine, backup_id, principal_id, approval_action_id):
    """R51 P0-8: 使用 BackupEngine._compute_restore_request_hash 计算 hash。

    hash 绑定: backup_id + target + schema_version + requested_by + approval_id
    """
    from services.backup_engine import MANIFEST_SCHEMA_VERSION
    return engine._compute_restore_request_hash(
        backup_id=backup_id,
        target="production",
        schema_version=MANIFEST_SCHEMA_VERSION,
        requested_by=principal_id,
        approval_id=approval_action_id,
    )


# ════════════════════════════════════════════════════════════════
# 1. production restore 无 expected_request_hash → raise AppError
# ════════════════════════════════════════════════════════════════

class TestProductionRestoreHashRequired:
    """R51 P0-8 场景 1: production restore 无 expected_request_hash 应 raise AppError。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_without_hash_raises_apperror(
        self, real_store_with_engine, monkeypatch,
    ):
        """production restore 未传 expected_request_hash → PRODUCTION_RESTORE_HASH_REQUIRED。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "r51_p0_8_no_hash_001"
        principal_id = 1001
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash="any_hash",
        )

        # 不传 expected_request_hash(默认 None)→ raise AppError
        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=approval_action_id,
                # expected_request_hash 不传
            )

        # 验证错误码
        assert exc_info.value.code == ErrorCodes.PRODUCTION_RESTORE_HASH_REQUIRED, (
            f"期望 PRODUCTION_RESTORE_HASH_REQUIRED,实际: {exc_info.value.code}"
        )


# ════════════════════════════════════════════════════════════════
# 2. production restore hash 不匹配 → raise AppError
# ════════════════════════════════════════════════════════════════

class TestProductionRestoreHashMismatch:
    """R51 P0-8 场景 2: production restore hash 不匹配应 raise AppError。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_mismatched_hash_raises_apperror(
        self, real_store_with_engine, monkeypatch,
    ):
        """production restore expected_request_hash 与 stored_hash 不匹配 → MISMATCH。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "r51_p0_8_mismatch_001"
        principal_id = 1002
        # 存储正确的 hash
        correct_hash = _compute_hash(engine, backup_id, principal_id, approval_action_id)
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash=correct_hash,
        )

        # mock db_restore(不应被调用,因为校验失败)
        call_count = {"n": 0}
        async def _spy_restore(*args, **kwargs):
            call_count["n"] += 1
            return {"restored_tables": 0}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _spy_restore,
        )
        monkeypatch.setattr(
            "services.restore_writer._restore_from_backup_data",
            _spy_restore,
        )

        # 传入不匹配的 hash → raise AppError(PRODUCTION_RESTORE_HASH_MISMATCH)
        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=approval_action_id,
                expected_request_hash="tampered_hash_999",
            )

        assert exc_info.value.code == ErrorCodes.PRODUCTION_RESTORE_HASH_MISMATCH, (
            f"期望 PRODUCTION_RESTORE_HASH_MISMATCH,实际: {exc_info.value.code}"
        )
        # 校验 db_restore 未被调用
        assert call_count["n"] == 0, "TOCTOU 校验失败时不应调用 db_restore"


# ════════════════════════════════════════════════════════════════
# 3. production restore hash 匹配 → 执行成功
# ════════════════════════════════════════════════════════════════

class TestProductionRestoreHashMatch:
    """R51 P0-8 场景 3: production restore hash 匹配应执行成功。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_matching_hash_succeeds(
        self, real_store_with_engine, monkeypatch,
    ):
        """production restore 传入匹配的 expected_request_hash → 恢复成功。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "r51_p0_8_match_001"
        principal_id = 1003
        # 存储正确的 hash
        correct_hash = _compute_hash(engine, backup_id, principal_id, approval_action_id)
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash=correct_hash,
        )

        # mock db_restore
        async def _fake_restore(*args, **kwargs):
            return {"restored_tables": 2, "restored_rows": 3}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _fake_restore,
        )
        monkeypatch.setattr(
            "services.restore_writer._restore_from_backup_data",
            _fake_restore,
        )

        # 传入匹配的 hash → 应成功
        result = await engine.restore(
            backup_id, target="production",
            approver_id=principal_id,
            approval_action_id=approval_action_id,
            expected_request_hash=correct_hash,
        )

        assert result["success"] is True
        assert result["restored_tables"] == 2


# ════════════════════════════════════════════════════════════════
# 4. command_executions status='executed' → raise AppError(RESTORE_ALREADY_EXECUTED)
# ════════════════════════════════════════════════════════════════

class TestRestoreAlreadyExecuted:
    """R51 P0-8 场景 4: status='executed' 时拒绝重复执行(RESTORE_ALREADY_EXECUTED)。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_rejected_when_status_executed(
        self, real_store_with_engine, monkeypatch,
    ):
        """command_executions.status='executed' → RESTORE_ALREADY_EXECUTED。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "r51_p0_8_executed_001"
        principal_id = 1004
        correct_hash = _compute_hash(engine, backup_id, principal_id, approval_action_id)
        # 插入 status='executed'(恢复已完成)
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="executed", request_hash=correct_hash,
        )

        # mock db_restore(不应被调用)
        call_count = {"n": 0}
        async def _spy_restore(*args, **kwargs):
            call_count["n"] += 1
            return {"restored_tables": 0}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _spy_restore,
        )
        monkeypatch.setattr(
            "services.restore_writer._restore_from_backup_data",
            _spy_restore,
        )

        # status='executed' → raise AppError(RESTORE_ALREADY_EXECUTED)
        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=approval_action_id,
                expected_request_hash=correct_hash,
            )

        assert exc_info.value.code == ErrorCodes.RESTORE_ALREADY_EXECUTED, (
            f"期望 RESTORE_ALREADY_EXECUTED,实际: {exc_info.value.code}"
        )
        # db_restore 不应被调用
        assert call_count["n"] == 0, "已 executed 的恢复不应再次调用 db_restore"


# ════════════════════════════════════════════════════════════════
# 5. restore 成功后 status 更新为 'executed'
# ════════════════════════════════════════════════════════════════

class TestRestoreUpdatesStatusToExecuted:
    """R51 P0-8 场景 5: restore 成功后 command_executions.status 更新为 'executed'。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_success_updates_status_to_executed(
        self, real_store_with_engine, monkeypatch,
    ):
        """restore 成功后 status 从 'approved' 更新为 'executed'。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "r51_p0_8_status_update_001"
        principal_id = 1005
        correct_hash = _compute_hash(engine, backup_id, principal_id, approval_action_id)
        # 插入 status='approved'(审批通过等待执行)
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash=correct_hash,
        )

        # mock db_restore
        async def _fake_restore(*args, **kwargs):
            return {"restored_tables": 2, "restored_rows": 3}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _fake_restore,
        )
        monkeypatch.setattr(
            "services.restore_writer._restore_from_backup_data",
            _fake_restore,
        )

        # 执行 restore(应成功)
        result = await engine.restore(
            backup_id, target="production",
            approver_id=principal_id,
            approval_action_id=approval_action_id,
            expected_request_hash=correct_hash,
        )
        assert result["success"] is True

        # 验证 command_executions.status 已更新为 'executed'
        cursor = await store._db.execute(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (approval_action_id,),
        )
        row = await cursor.fetchone()
        assert row is not None, "command_executions 记录应存在"
        assert row[0] == "executed", (
            f"restore 成功后 status 应为 'executed',实际: {row[0]}"
        )


# ════════════════════════════════════════════════════════════════
# 6. 非 production 模式 → hash 可选(向后兼容)
# ════════════════════════════════════════════════════════════════

class TestNonProductionHashOptional:
    """R51 P0-8 场景 6: 非 production 模式 expected_request_hash 可选(向后兼容)。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_staging_restore_without_hash_succeeds(
        self, real_store_with_engine, monkeypatch,
    ):
        """staging 模式不传 expected_request_hash → 恢复成功(向后兼容)。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        # mock db_restore(staging 模式不写库,但仍需 mock 避免 ImportError)
        async def _fake_restore(*args, **kwargs):
            return {"restored_tables": 0, "restored_rows": 0}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _fake_restore,
        )
        monkeypatch.setattr(
            "services.restore_writer._restore_from_backup_data",
            _fake_restore,
        )

        # staging 模式不传 expected_request_hash → 应成功(不强制 hash)
        result = await engine.restore(
            backup_id, target="staging",
            # 不传 approval_action_id / expected_request_hash
        )

        assert result["success"] is True
        assert result["checksum_verified"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_test_mode_restore_without_hash_succeeds(
        self, real_store_with_engine, monkeypatch,
    ):
        """test 模式不传 expected_request_hash → 恢复成功(仅校验可解密)。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        # test 模式不传 expected_request_hash → 应成功
        result = await engine.restore(
            backup_id, target="test",
            # 不传 approval_action_id / expected_request_hash
        )

        assert result["success"] is True
        assert result["checksum_verified"] is True
