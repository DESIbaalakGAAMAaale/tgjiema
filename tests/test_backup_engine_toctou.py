"""R44 G0-1: BackupEngine TOCTOU 修复测试。

测试覆盖:
- _validate_production_approval: request_hash 比对(TOCTOU 防护)
- restore: expected_request_hash 参数透传
- restore: approver_id=None 时从 command_executions 反查 principal_id
- restore: 向后兼容(不传 expected_request_hash 仍能工作)

测试策略:
- Mock R2 storage(避免真实 R2 调用)
- 使用真实 SQLite 临时数据库(校验 command_executions 表查询)
- 使用真实 backup_crypto(AES-256-GCM)加密
"""
from __future__ import annotations

import hashlib
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
                    "last_modified": "2026-07-13T10:00:00.000Z",
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

    tmpdir = tempfile.mkdtemp(prefix="r44_toctou_test_")
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


async def _seed_command_executions(
    store,
    action_id: str,
    principal_id: int,
    status: str = "executed",
    request_hash: str = "d" * 64,  # R55 P0-2: 64 位 hex 格式
) -> None:
    """向 command_executions 表插入一条记录(用于 _validate_production_approval 校验)。"""
    now = "2026-07-13T10:00:00"
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


# ════════════════════════════════════════════════════════════════
# 1. _validate_production_approval TOCTOU 校验
# ════════════════════════════════════════════════════════════════

class TestValidateProductionApprovalRequestHash:
    """R44 G0-1: _validate_production_approval 的 request_hash(TOCTOU)校验。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_validate_production_approval_checks_request_hash(
        self, real_store_with_engine, monkeypatch,
    ):
        """_validate_production_approval 应校验 expected_request_hash 与存储的 hash 一致。

        场景: 审批通过后 payload 被替换(TOCTOU 攻击),request_hash 不匹配应抛 PermissionError。
        """
        store, engine, _, _ = real_store_with_engine
        approval_action_id = "toctou_action_001"
        principal_id = 999
        stored_hash = "5" * 64  # R55 P0-2: 64 位 hex 格式
        # R51 P0-8: status='approved' 表示审批通过等待执行(非 executed)
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash=stored_hash,
        )

        # 1. expected_request_hash 与 stored_hash 一致 → 校验通过(不抛异常)
        await engine._validate_production_approval(
            approver_id=principal_id,
            approval_action_id=approval_action_id,
            expected_request_hash=stored_hash,
        )

        # 2. expected_request_hash 与 stored_hash 不一致 → 抛 AppError(TOCTOU, R51 P0-8 协议化)
        # R55 P0-2: request_hash 必须为 64 位 hex 格式
        with pytest.raises(AppError) as exc_info:
            await engine._validate_production_approval(
                approver_id=principal_id,
                approval_action_id=approval_action_id,
                expected_request_hash="e" * 64,  # 64 位 hex 但与 stored 不匹配
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_RESTORE_HASH_MISMATCH

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_validate_production_approval_skips_hash_when_none(
        self, real_store_with_engine, monkeypatch,
    ):
        """expected_request_hash=None 时应跳过 TOCTOU 校验(向后兼容)。"""
        store, engine, _, _ = real_store_with_engine
        approval_action_id = "toctou_skip_001"
        principal_id = 888
        # R51 P0-8: status='approved' 表示审批通过等待执行
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash="f" * 64,  # R55 P0-2: 64 位 hex
        )

        # 不传 expected_request_hash → 跳过 TOCTOU 校验,应通过
        await engine._validate_production_approval(
            approver_id=principal_id,
            approval_action_id=approval_action_id,
            expected_request_hash=None,
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_validate_production_approval_rejects_empty_stored_hash(
        self, real_store_with_engine, monkeypatch,
    ):
        """command_executions.request_hash 为空时,应 fail-closed 拒绝。"""
        store, engine, _, _ = real_store_with_engine
        approval_action_id = "toctou_empty_stored_001"
        principal_id = 777
        # request_hash 存空字符串(模拟旧记录无 hash 字段)
        # R51 P0-8: status='approved' 表示审批通过等待执行
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash="",
        )

        # expected_request_hash 非空,但 stored_hash 为空 → fail-closed
        # R55 P0-2: expected_request_hash 必须为 64 位 hex 格式
        with pytest.raises(PermissionError, match="request_hash 为空"):
            await engine._validate_production_approval(
                approver_id=principal_id,
                approval_action_id=approval_action_id,
                expected_request_hash="1" * 64,  # 64 位 hex
            )


# ════════════════════════════════════════════════════════════════
# 2. restore() 整体流程的 TOCTOU 校验
# ════════════════════════════════════════════════════════════════

class TestRestoreRequestHash:
    """R44 G0-1: restore() 的 expected_request_hash 参数透传。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_with_matching_request_hash_succeeds(
        self, real_store_with_engine, monkeypatch,
    ):
        """restore() 传入匹配的 expected_request_hash 应成功恢复。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "restore_match_001"
        principal_id = 123
        request_hash = "a" * 64  # R55 P0-2: 64 位 hex 格式
        # R51 P0-8: status='approved' 表示审批通过等待执行
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash=request_hash,
        )

        # mock db_restore 避免真实写库
        async def _fake_restore_from_backup_data(*args, **kwargs):
            return {"restored_tables": 2, "restored_rows": 3}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _fake_restore_from_backup_data,
        )

        # 传入 matching expected_request_hash → 应成功
        result = await engine.restore(
            backup_id, target="production",
            approver_id=principal_id,
            approval_action_id=approval_action_id,
            expected_request_hash=request_hash,
        )

        assert result["success"] is True
        assert result["restored_tables"] == 2

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_with_mismatched_request_hash_raises_permission_error(
        self, real_store_with_engine, monkeypatch,
    ):
        """restore() 传入不匹配的 expected_request_hash 应抛 PermissionError(TOCTOU)。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "restore_mismatch_001"
        principal_id = 456
        stored_hash = "b" * 64  # R55 P0-2: 64 位 hex 格式
        # R51 P0-8: status='approved' 表示审批通过等待执行
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash=stored_hash,
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

        # 传入不匹配的 expected_request_hash → 抛 AppError(R51 P0-8 协议化)
        # R55 P0-2: request_hash 必须为 64 位 hex 格式
        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=approval_action_id,
                expected_request_hash="c" * 64,  # 64 位 hex 但与 stored_hash 不匹配
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_RESTORE_HASH_MISMATCH

        # 校验 db_restore 未被调用
        assert call_count["n"] == 0, "TOCTOU 校验失败时不应调用 db_restore"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_without_request_hash_now_required(
        self, real_store_with_engine, monkeypatch,
    ):
        """R51 P0-8: production 恢复不传 expected_request_hash 时应抛 AppError(TOCTOU 防护不可绕过)。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "restore_no_hash_001"
        principal_id = 789
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash="2" * 64,  # R55 P0-2: 64 位 hex
        )

        # R51 P0-8: 不传 expected_request_hash → 抛 AppError(PRODUCTION_RESTORE_HASH_REQUIRED)
        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=approval_action_id,
            )
        assert exc_info.value.code == ErrorCodes.PRODUCTION_RESTORE_HASH_REQUIRED


# ════════════════════════════════════════════════════════════════
# 3. restore() 反查 principal_id(R44 G0-3)
# ════════════════════════════════════════════════════════════════

class TestRestorePrincipalLookup:
    """R44 G0-3: restore() 不传 approver_id 时从 command_executions 反查 principal_id。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_principals_id_reflect_from_command_executions(
        self, real_store_with_engine, monkeypatch,
    ):
        """不传 approver_id(None)时,restore 应从 command_executions 反查 principal_id。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        approval_action_id = "restore_lookup_001"
        principal_id = 555
        # R51 P0-8: status='approved' + 传 expected_request_hash
        await _seed_command_executions(
            store, approval_action_id, principal_id,
            status="approved", request_hash="3" * 64,  # R55 P0-2: 64 位 hex
        )

        # mock db_restore
        async def _fake_restore(*args, **kwargs):
            return {"restored_tables": 1, "restored_rows": 1}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _fake_restore,
        )

        # 不传 approver_id(None),让 restore 通过 _lookup_principal_id 反查
        # R51 P0-8: 传 expected_request_hash 与 stored hash 一致
        result = await engine.restore(
            backup_id, target="production",
            approval_action_id=approval_action_id,
            expected_request_hash="3" * 64,  # R55 P0-2: 64 位 hex 与 stored 一致
        )

        assert result["success"] is True
        assert result["restored_tables"] == 2  # R44 fixup: mock 数据含 users + file_records 两表

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_lookup_principal_id_returns_zero_for_nonexistent(
        self, real_store_with_engine, monkeypatch,
    ):
        """approval_action_id 不在 command_executions 中时,_lookup_principal_id 返回 0。"""
        store, engine, _, _ = real_store_with_engine

        result = await engine._lookup_principal_id("nonexistent_action_id")
        assert result == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_raises_when_principal_lookup_fails(
        self, real_store_with_engine, monkeypatch,
    ):
        """approver_id=None 且反查 principal_id 失败时,restore 应抛 PermissionError。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        # 不在 command_executions 中插入记录 → _lookup_principal_id 返回 0
        # R51 P0-8: 需传 expected_request_hash 才能到达 principal 反查路径
        # restore 应抛 PermissionError(fail-closed)
        with pytest.raises(PermissionError, match="无法从 command_executions 反查 principal_id"):
            await engine.restore(
                backup_id, target="production",
                approval_action_id="missing_action_id_001",
                expected_request_hash="4" * 64,  # R55 P0-2: 64 位 hex
            )


# ════════════════════════════════════════════════════════════════
# 4. disaster_recovery 强制 approval_action_id
# ════════════════════════════════════════════════════════════════

class TestDisasterRestoreApprovalGate:
    """R44 G0-3: disaster_recovery.restore 必须传入 approval_action_id。"""

    @pytest.mark.asyncio
    async def test_disaster_restore_raises_without_approval_action_id(self, monkeypatch):
        """不传 approval_action_id 时,disaster_recovery.restore 应抛 AppError。"""
        from services import disaster_recovery

        with pytest.raises(AppError) as exc_info:
            await disaster_recovery.restore(
                backup_id="backup_20260713_120000_abcd1234",
                approver_id=999,
            )
        assert exc_info.value.envelope.code == ErrorCodes.BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED

    @pytest.mark.asyncio
    async def test_disaster_restore_raises_with_empty_backup_id(self, monkeypatch):
        """空 backup_id 应返回失败字典(不调用 engine.restore)。"""
        from services import disaster_recovery

        result = await disaster_recovery.restore(
            backup_id="",
            approval_action_id="some_action_id",
        )
        assert result["success"] is False
        assert "backup_id 为空" in result["error"]
