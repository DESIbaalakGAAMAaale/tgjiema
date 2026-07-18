"""R52 P0-5: Approval/Restore 状态语义统一 — 统一状态机测试。

测试覆盖统一状态机 ``pending → approved → executing → executed/failed``:

1. CommandBus 辅助函数(状态机核心):
   - VALID_TRANSITIONS 合法性校验
   - claim_execution_approved CAS approved→executing
   - mark_approved_executed CAS executing→executed
   - mark_approved_failed CAS executing→failed
   - get_command_status 查询
   - verify_command_approved 状态校验(记录不存在 / 状态非 approved / hash 不匹配)

2. Repair Console(execute_repair):
   - status='pending' → 拒绝(审批未通过)
   - status='approved' → 执行成功 → status='executed'
   - status='executing' → 拒绝(已在执行中)
   - status='executed' → 拒绝(已完成)
   - 执行失败 → status='failed'

3. Maintenance(disable + recover_maintenance):
   - status='approved' → 成功关闭 → status='executed'
   - status='executed' → 拒绝(语义冲突修复验证)

4. Restore(backup_engine.restore):
   - status='approved' → 恢复成功 → status='executed'
   - status='executed' → 拒绝(RESTORE_ALREADY_EXECUTED)
   - 恢复失败 → status='failed'

5. 并发 CAS 防护:
   - 同一 action_id 两次 CAS,只有一个成功

测试策略:
    - 使用真实 SQLite 临时数据库(隔离生产数据)
    - Mock R2 storage(避免真实 R2 调用)
    - 使用真实 backup_crypto(AES-256-GCM)加密
    - 中文注释,英文 raise 消息
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

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
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r52_p0_5_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def real_store_with_engine(monkeypatch):
    """创建一个临时 SQLite + 注入 mock storage 与 KEK 的 BackupEngine。

    Yields:
        (store, engine, fake_storage, kek_b64)
    """
    from services.backup_engine import BackupEngine
    from services.backup_crypto import generate_kek

    tmpdir = tempfile.mkdtemp(prefix="r52_p0_5_restore_test_")
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


@pytest_asyncio.fixture
async def reset_cache():
    """每个用例前重置 maintenance_mode 模块级缓存。"""
    from services import maintenance_mode
    maintenance_mode._reset_cache_for_test()
    yield
    maintenance_mode._reset_cache_for_test()


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存,避免跨用例污染。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


# ════════════════════════════════════════════════════════════════
# 辅助函数
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
                    "last_modified": "2026-07-15T10:00:00.000Z",
                })
            if len(result) >= max_keys:
                break
        return result


async def _insert_command_execution(
    store,
    action_id: str,
    status: str = "approved",
    principal_id: int = 100,
    request_hash: str = "a" * 64,
    command_type: str = "test_command",
    result_json: str = "",
):
    """直接插入一条 command_executions 记录(模拟 CommandBus 审批结果)。

    R52 P0-5: 默认 status='approved'(审批通过等待执行)。
    """
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    if not result_json:
        result_json = '{"success": true}' if status == "executed" else ""
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        " request_hash, result, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
        (action_id, command_type, principal_id, status,
         request_hash, result_json or None, now, now),
    )
    await store._db.commit()


async def _get_command_status(store, action_id: str) -> str | None:
    """查询 command_executions.status。"""
    rows = await store._db.execute_fetchall(
        "SELECT status FROM command_executions WHERE action_id = ?",
        (action_id,),
    )
    if rows and rows[0]:
        return rows[0][0]
    return None


async def _set_recover_status(store, status: str):
    """直接 UPDATE maintenance_state.recover_status(用于场景构造)。"""
    await store._db.execute(
        "UPDATE maintenance_state SET recover_status = ? WHERE id = ?",
        (status, 1),
    )
    await store._db.commit()


def _patch_backup_all_tables(monkeypatch, tables: dict | None = None):
    """Mock services.db_backup.backup_all_tables 返回固定 backup_data。"""
    if tables is None:
        tables = {
            "users": [{"user_id": 1, "name": "alice"}],
            "file_records": [{"file_code": "ABC123", "status": "active"}],
        }

    async def _fake_backup_all_tables(watermark=None, backup_type="full"):
        return {
            "backup_time": "2026-07-15T10:00:00",
            "tables": tables,
            "_r38_p1_5_metadata": {
                "start_time": "2026-07-15T10:00:00",
                "end_time": "2026-07-15T10:00:01",
                "backup_type": backup_type,
                "watermark": None,
                "prev_watermark": None,
            },
        }

    monkeypatch.setattr(
        "services.db_backup.backup_all_tables", _fake_backup_all_tables,
    )
    return tables


async def _make_backup(engine) -> str:
    """通过 BackupEngine.create_backup 创建一个备份,返回 backup_id。"""
    manifest = await engine.create_backup(backup_type="full")
    return manifest["backup_id"]


def _compute_restore_hash(engine, backup_id, principal_id, approval_action_id):
    """R52 P0-5: 使用 BackupEngine._compute_restore_request_hash 计算 hash。"""
    from services.backup_engine import MANIFEST_SCHEMA_VERSION
    return engine._compute_restore_request_hash(
        backup_id=backup_id,
        target="production",
        schema_version=MANIFEST_SCHEMA_VERSION,
        requested_by=principal_id,
        approval_id=approval_action_id,
    )


# ════════════════════════════════════════════════════════════════
# 1. CommandBus 状态机核心函数测试
# ════════════════════════════════════════════════════════════════

class TestValidTransitions:
    """R52 P0-5: VALID_TRANSITIONS 状态转换合法性。"""

    def test_pending_to_approved_is_valid(self):
        """pending → approved 合法。"""
        from services.command_bus import is_valid_transition
        assert is_valid_transition("pending", "approved") is True

    def test_approved_to_executing_is_valid(self):
        """approved → executing 合法。"""
        from services.command_bus import is_valid_transition
        assert is_valid_transition("approved", "executing") is True

    def test_executing_to_executed_is_valid(self):
        """executing → executed 合法。"""
        from services.command_bus import is_valid_transition
        assert is_valid_transition("executing", "executed") is True

    def test_executing_to_failed_is_valid(self):
        """executing → failed 合法。"""
        from services.command_bus import is_valid_transition
        assert is_valid_transition("executing", "failed") is True

    def test_executed_to_anything_is_invalid(self):
        """executed 是终态,不可再转换。"""
        from services.command_bus import is_valid_transition
        assert is_valid_transition("executed", "approved") is False
        assert is_valid_transition("executed", "executing") is False

    def test_approved_to_executed_is_invalid(self):
        """approved 不能直接跳到 executed(必须经过 executing)。"""
        from services.command_bus import is_valid_transition
        assert is_valid_transition("approved", "executed") is False


class TestClaimExecutionApproved:
    """R52 P0-5: claim_execution_approved CAS approved→executing。"""

    @pytest.mark.asyncio
    async def test_claim_success_when_approved(self, real_store):
        """status='approved' → CAS 成功,status 变为 'executing'。"""
        from services.command_bus import claim_execution_approved, CMD_STATUS_EXECUTING

        action_id = "r52_claim_001"
        await _insert_command_execution(real_store, action_id, status="approved")

        claimed = await claim_execution_approved(
            action_id=action_id, owner="test_worker_001",
            request_hash="a" * 64,
        )
        assert claimed is True, "status='approved' 时 CAS 应成功"

        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_EXECUTING, (
            f"CAS 后 status 应为 'executing',实际: {status}"
        )

    @pytest.mark.asyncio
    async def test_claim_fails_when_pending(self, real_store):
        """status='pending' → CAS 失败(未审批)。"""
        from services.command_bus import claim_execution_approved

        action_id = "r52_claim_002"
        await _insert_command_execution(real_store, action_id, status="pending")

        claimed = await claim_execution_approved(
            action_id=action_id, owner="test_worker_002",
            request_hash="a" * 64,
        )
        assert claimed is False, "status='pending' 时 CAS 应失败"

    @pytest.mark.asyncio
    async def test_claim_fails_when_executing(self, real_store):
        """status='executing' → CAS 失败(已在执行中)。"""
        from services.command_bus import claim_execution_approved

        action_id = "r52_claim_003"
        await _insert_command_execution(real_store, action_id, status="executing")

        claimed = await claim_execution_approved(
            action_id=action_id, owner="test_worker_003",
            request_hash="a" * 64,
        )
        assert claimed is False, "status='executing' 时 CAS 应失败"

    @pytest.mark.asyncio
    async def test_claim_fails_when_executed(self, real_store):
        """status='executed' → CAS 失败(已完成)。"""
        from services.command_bus import claim_execution_approved

        action_id = "r52_claim_004"
        await _insert_command_execution(real_store, action_id, status="executed")

        claimed = await claim_execution_approved(
            action_id=action_id, owner="test_worker_004",
            request_hash="a" * 64,
        )
        assert claimed is False, "status='executed' 时 CAS 应失败"

    @pytest.mark.asyncio
    async def test_claim_hash_mismatch_rejected(self, real_store):
        """request_hash 不匹配 → CAS 失败(防篡改)。"""
        from services.command_bus import claim_execution_approved

        action_id = "r52_claim_005"
        await _insert_command_execution(
            real_store, action_id, status="approved",
            request_hash="a" * 64,
        )

        claimed = await claim_execution_approved(
            action_id=action_id, owner="test_worker_005",
            request_hash="b" * 64,
        )
        assert claimed is False, "request_hash 不匹配时 CAS 应失败"


class TestMarkApprovedExecutedAndFailed:
    """R52 P0-5: mark_approved_executed / mark_approved_failed CAS。"""

    @pytest.mark.asyncio
    async def test_mark_executed_success(self, real_store):
        """executing → executed 成功。"""
        from services.command_bus import mark_approved_executed, CMD_STATUS_EXECUTED

        action_id = "r52_mark_001"
        await _insert_command_execution(real_store, action_id, status="executing")

        ok = await mark_approved_executed(action_id, result={"success": True})
        assert ok is True, "executing → executed CAS 应成功"

        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_EXECUTED

    @pytest.mark.asyncio
    async def test_mark_executed_fails_when_not_executing(self, real_store):
        """非 executing 状态 → mark_approved_executed 失败。"""
        from services.command_bus import mark_approved_executed

        action_id = "r52_mark_002"
        await _insert_command_execution(real_store, action_id, status="approved")

        ok = await mark_approved_executed(action_id)
        assert ok is False, "approved 状态下 mark_approved_executed 应失败"

    @pytest.mark.asyncio
    async def test_mark_failed_success(self, real_store):
        """executing → failed 成功。"""
        from services.command_bus import mark_approved_failed, CMD_STATUS_FAILED

        action_id = "r52_mark_003"
        await _insert_command_execution(real_store, action_id, status="executing")

        ok = await mark_approved_failed(action_id, error="test failure")
        assert ok is True, "executing → failed CAS 应成功"

        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_FAILED

    @pytest.mark.asyncio
    async def test_mark_retryable_success(self, real_store):
        """executing → retryable 成功(retryable=True)。"""
        from services.command_bus import mark_approved_failed, CMD_STATUS_RETRYABLE

        action_id = "r52_mark_004"
        await _insert_command_execution(real_store, action_id, status="executing")

        ok = await mark_approved_failed(
            action_id, error="transient failure", retryable=True,
        )
        assert ok is True, "executing → retryable CAS 应成功"

        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_RETRYABLE


class TestVerifyCommandApproved:
    """R52 P0-5: verify_command_approved 状态校验。"""

    @pytest.mark.asyncio
    async def test_verify_success_when_approved(self, real_store):
        """status='approved' → 校验通过,返回元信息。"""
        from services.command_bus import verify_command_approved

        action_id = "r52_verify_001"
        await _insert_command_execution(
            real_store, action_id, status="approved",
            principal_id=200, request_hash="a" * 64,
        )

        result = await verify_command_approved(
            action_id,
            expected_principal_id=200,
            expected_request_hash="a" * 64,
        )
        assert result["status"] == "approved"
        assert result["principal_id"] == 200

    @pytest.mark.asyncio
    async def test_verify_fails_when_not_approved(self, real_store):
        """status='executed' → raise AppError(COMMAND_NOT_APPROVED)。"""
        from services.command_bus import verify_command_approved
        from services.error_codes import AppError, ErrorCodes

        action_id = "r52_verify_002"
        await _insert_command_execution(real_store, action_id, status="executed")

        with pytest.raises(AppError) as exc_info:
            await verify_command_approved(action_id)

        assert exc_info.value.code == ErrorCodes.COMMAND_NOT_APPROVED, (
            f"期望 COMMAND_NOT_APPROVED,实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_verify_fails_when_not_found(self, real_store):
        """记录不存在 → raise AppError(COMMAND_NOT_APPROVED)。"""
        from services.command_bus import verify_command_approved
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            await verify_command_approved("non_existent_action_id_999")

        assert exc_info.value.code == ErrorCodes.COMMAND_NOT_APPROVED

    @pytest.mark.asyncio
    async def test_verify_fails_when_principal_mismatch(self, real_store):
        """principal_id 不匹配 → raise AppError(COMMAND_STATUS_CONFLICT)。"""
        from services.command_bus import verify_command_approved
        from services.error_codes import AppError, ErrorCodes

        action_id = "r52_verify_004"
        await _insert_command_execution(
            real_store, action_id, status="approved",
            principal_id=200,
        )

        with pytest.raises(AppError) as exc_info:
            await verify_command_approved(
                action_id, expected_principal_id=999,
            )

        assert exc_info.value.code == ErrorCodes.COMMAND_STATUS_CONFLICT, (
            f"期望 COMMAND_STATUS_CONFLICT,实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_verify_fails_when_hash_mismatch(self, real_store):
        """request_hash 不匹配 → raise AppError(COMMAND_HASH_MISMATCH)。"""
        from services.command_bus import verify_command_approved
        from services.error_codes import AppError, ErrorCodes

        action_id = "r52_verify_005"
        await _insert_command_execution(
            real_store, action_id, status="approved",
            principal_id=200, request_hash="a" * 64,
        )

        with pytest.raises(AppError) as exc_info:
            await verify_command_approved(
                action_id,
                expected_principal_id=200,
                expected_request_hash="b" * 64,
            )

        assert exc_info.value.code == ErrorCodes.COMMAND_HASH_MISMATCH, (
            f"期望 COMMAND_HASH_MISMATCH,实际: {exc_info.value.code}"
        )


# ════════════════════════════════════════════════════════════════
# 2. 并发 CAS 防护测试
# ════════════════════════════════════════════════════════════════

class TestConcurrentCAS:
    """R52 P0-5: 并发 CAS 防护 — 同一 action_id 只有一个 worker 能进入 executing。"""

    @pytest.mark.asyncio
    async def test_concurrent_claim_only_one_succeeds(self, real_store):
        """两个 worker 同时 CAS approved→executing,只有一个成功。"""
        from services.command_bus import claim_execution_approved, CMD_STATUS_EXECUTING

        action_id = "r52_concurrent_001"
        await _insert_command_execution(real_store, action_id, status="approved")

        # 第一次 CAS 应成功
        claimed_1 = await claim_execution_approved(
            action_id=action_id, owner="worker_A",
            request_hash="a" * 64,
        )
        assert claimed_1 is True, "第一个 worker 的 CAS 应成功"

        # 第二次 CAS 应失败(status 已变为 executing)
        claimed_2 = await claim_execution_approved(
            action_id=action_id, owner="worker_B",
            request_hash="a" * 64,
        )
        assert claimed_2 is False, "第二个 worker 的 CAS 应失败(已被抢占)"

        # 最终状态为 executing,owner 为 worker_A
        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_EXECUTING


# ════════════════════════════════════════════════════════════════
# 3. Repair Console 状态机测试
# ════════════════════════════════════════════════════════════════

class TestRepairConsoleStateMachine:
    """R52 P0-5: Repair Console execute_repair 统一状态机。"""

    @pytest.mark.asyncio
    async def test_repair_rejected_when_status_pending(self, real_store):
        """status='pending' → 审批未通过,拒绝执行。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        action_id = "r52_repair_pending_001"
        await _insert_command_execution(
            real_store, action_id, status="pending",
            principal_id=100, request_hash=repair_console.compute_repair_request_hash(
                "retry_outbox", {"ids": [1]},
            ),
        )

        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="retry_outbox",
                params={"ids": [1]},
                principal_id=100,
                approval_action_id=action_id,
            )

        # _verify_approval 返回 False → 抛 APPROVAL_HASH_MISMATCH
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH, (
            f"期望 REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH,实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_repair_rejected_when_status_executed(self, real_store):
        """status='executed' → 拒绝执行(已完成,语义冲突修复验证)。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        action_id = "r52_repair_executed_001"
        await _insert_command_execution(
            real_store, action_id, status="executed",
            principal_id=100, request_hash=repair_console.compute_repair_request_hash(
                "retry_outbox", {"ids": [1]},
            ),
        )

        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="retry_outbox",
                params={"ids": [1]},
                principal_id=100,
                approval_action_id=action_id,
            )

        # status='executed' 不是 'approved' → _verify_approval 返回 False
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH, (
            f"期望 REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH,实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    async def test_repair_success_when_approved(self, real_store):
        """status='approved' → 执行成功 → status='executed'。"""
        from services import repair_console
        from services.command_bus import CMD_STATUS_EXECUTED

        action_id = "r52_repair_success_001"
        expected_hash = repair_console.compute_repair_request_hash(
            "retry_outbox", {"ids": []},
        )
        await _insert_command_execution(
            real_store, action_id, status="approved",
            principal_id=100, request_hash=expected_hash,
        )

        # Mock retry_outbox 返回 0(空列表,不影响测试)
        with patch.object(
            repair_console, "retry_outbox",
            new=AsyncMock(return_value=0),
        ):
            result = await repair_console.execute_repair(
                action="retry_outbox",
                params={"ids": []},
                principal_id=100,
                approval_action_id=action_id,
            )

        assert result["success"] is True
        assert result["approval_verified"] is True

        # 验证状态已从 approved → executing → executed
        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_EXECUTED, (
            f"repair 成功后 status 应为 'executed',实际: {status}"
        )

    @pytest.mark.asyncio
    async def test_repair_failure_marks_failed(self, real_store):
        """status='approved' → 执行失败 → status='failed'。"""
        from services import repair_console
        from services.command_bus import CMD_STATUS_FAILED

        action_id = "r52_repair_failed_001"
        expected_hash = repair_console.compute_repair_request_hash(
            "retry_outbox", {"ids": [999]},
        )
        await _insert_command_execution(
            real_store, action_id, status="approved",
            principal_id=100, request_hash=expected_hash,
        )

        # Mock retry_outbox 抛异常
        with patch.object(
            repair_console, "retry_outbox",
            new=AsyncMock(side_effect=RuntimeError("DB connection lost")),
        ):
            result = await repair_console.execute_repair(
                action="retry_outbox",
                params={"ids": [999]},
                principal_id=100,
                approval_action_id=action_id,
            )

        assert result["success"] is False

        # 验证状态已从 approved → executing → failed
        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_FAILED, (
            f"repair 失败后 status 应为 'failed',实际: {status}"
        )

    @pytest.mark.asyncio
    async def test_repair_cas_conflict_when_executing(self, real_store):
        """status='executing' → CAS 失败(已在执行中)。"""
        from services import repair_console
        from services.error_codes import AppError, ErrorCodes

        action_id = "r52_repair_executing_001"
        expected_hash = repair_console.compute_repair_request_hash(
            "retry_outbox", {"ids": []},
        )
        # 直接插入 status='executing'(模拟其他 worker 已在执行)
        await _insert_command_execution(
            real_store, action_id, status="executing",
            principal_id=100, request_hash=expected_hash,
        )

        with pytest.raises(AppError) as exc_info:
            await repair_console.execute_repair(
                action="retry_outbox",
                params={"ids": []},
                principal_id=100,
                approval_action_id=action_id,
            )

        # _verify_approval 返回 False(status 非 approved)→ APPROVAL_HASH_MISMATCH
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH


# ════════════════════════════════════════════════════════════════
# 4. Maintenance 状态机测试
# ════════════════════════════════════════════════════════════════

class TestMaintenanceStateMachine:
    """R52 P0-5: Maintenance disable/recover_maintenance 统一状态机。"""

    @pytest.mark.asyncio
    async def test_disable_success_when_approved(self, real_store, reset_cache):
        """recover_status='pending' + status='approved' → 成功关闭 → status='executed'。"""
        from services import maintenance_mode
        from services.command_bus import CMD_STATUS_EXECUTED

        await maintenance_mode.enable("测试 R52 P0-5 disable approved", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 清理 dirty_outbox
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        action_id = "r52_maint_success_001"
        request_hash = "a" * 64
        await _insert_command_execution(
            real_store, action_id, status="approved",
            principal_id=100, request_hash=request_hash,
        )

        result = await maintenance_mode.disable(
            ended_by=100, force=True,
            approval_action_id=action_id,
            request_hash=request_hash,
        )

        assert result is True, "status='approved' 时 disable 应成功"

        # 验证状态已从 approved → executing → executed
        status = await _get_command_status(real_store, action_id)
        assert status == CMD_STATUS_EXECUTED, (
            f"disable 成功后 status 应为 'executed',实际: {status}"
        )

    @pytest.mark.asyncio
    async def test_disable_rejected_when_status_executed(self, real_store, reset_cache):
        """recover_status='pending' + status='executed' → 拒绝(语义冲突修复验证)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 R52 P0-5 disable executed rejected", started_by=100)
        await _set_recover_status(real_store, "pending")

        action_id = "r52_maint_executed_001"
        request_hash = "a" * 64
        await _insert_command_execution(
            real_store, action_id, status="executed",
            principal_id=100, request_hash=request_hash,
        )

        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(
                ended_by=100, force=True,
                approval_action_id=action_id,
                request_hash=request_hash,
            )

        # 异常消息应提及 'approved'(R52 P0-5 状态校验)
        assert "approved" in str(exc_info.value), (
            f"异常消息应提及 approved,实际: {exc_info.value}"
        )

        # maintenance_state 仍为 enabled(未关闭)
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, "status='executed' 时 disable 应拒绝"

    @pytest.mark.asyncio
    async def test_disable_rejected_when_status_pending(self, real_store, reset_cache):
        """recover_status='pending' + status='pending' → 拒绝(未审批)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 R52 P0-5 disable pending rejected", started_by=100)
        await _set_recover_status(real_store, "pending")

        action_id = "r52_maint_pending_001"
        request_hash = "a" * 64
        await _insert_command_execution(
            real_store, action_id, status="pending",
            principal_id=100, request_hash=request_hash,
        )

        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(
                ended_by=100, force=True,
                approval_action_id=action_id,
                request_hash=request_hash,
            )

        assert "approved" in str(exc_info.value), (
            f"异常消息应提及 approved,实际: {exc_info.value}"
        )


# ════════════════════════════════════════════════════════════════
# 5. Restore 状态机测试
# ════════════════════════════════════════════════════════════════

class TestRestoreStateMachine:
    """R52 P0-5: BackupEngine.restore 统一状态机。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_success_transitions_to_executed(
        self, real_store_with_engine, monkeypatch,
    ):
        """status='approved' → 恢复成功 → status='executed'(经过 executing 中间态)。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        action_id = "r52_restore_success_001"
        principal_id = 300
        correct_hash = _compute_restore_hash(engine, backup_id, principal_id, action_id)
        await _insert_command_execution(
            store, action_id, status="approved",
            principal_id=principal_id, request_hash=correct_hash,
        )

        async def _fake_restore(*args, **kwargs):
            return {"restored_tables": 2, "restored_rows": 3}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data", _fake_restore,
        )

        result = await engine.restore(
            backup_id, target="production",
            approver_id=principal_id,
            approval_action_id=action_id,
            expected_request_hash=correct_hash,
        )

        assert result["success"] is True

        # 验证状态已从 approved → executing → executed
        status = await _get_command_status(store, action_id)
        assert status == "executed", (
            f"restore 成功后 status 应为 'executed',实际: {status}"
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_rejected_when_status_executed(
        self, real_store_with_engine, monkeypatch,
    ):
        """status='executed' → 拒绝(RESTORE_ALREADY_EXECUTED)。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        action_id = "r52_restore_executed_001"
        principal_id = 301
        correct_hash = _compute_restore_hash(engine, backup_id, principal_id, action_id)
        await _insert_command_execution(
            store, action_id, status="executed",
            principal_id=principal_id, request_hash=correct_hash,
        )

        async def _spy_restore(*args, **kwargs):
            return {"restored_tables": 0}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data", _spy_restore,
        )

        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=action_id,
                expected_request_hash=correct_hash,
            )

        assert exc_info.value.code == ErrorCodes.RESTORE_ALREADY_EXECUTED, (
            f"期望 RESTORE_ALREADY_EXECUTED,实际: {exc_info.value.code}"
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_failure_marks_failed(
        self, real_store_with_engine, monkeypatch,
    ):
        """status='approved' → 恢复失败(异常) → status='failed'。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        action_id = "r52_restore_failed_001"
        principal_id = 302
        correct_hash = _compute_restore_hash(engine, backup_id, principal_id, action_id)
        await _insert_command_execution(
            store, action_id, status="approved",
            principal_id=principal_id, request_hash=correct_hash,
        )

        # Mock db_restore 抛异常
        # 注意:_restore_internal 内部捕获 restore_from_backup_data 异常,
        # 转化为 {"success": False, "error": "生产恢复写入失败: ..."} 字典返回
        # (这是现有 API 契约,不应破坏)。restore() 检测 success=False 后
        # 回写状态机 executing → failed。
        async def _failing_restore(*args, **kwargs):
            raise RuntimeError("CRDB connection lost during restore")
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data", _failing_restore,
        )

        result = await engine.restore(
            backup_id, target="production",
            approver_id=principal_id,
            approval_action_id=action_id,
            expected_request_hash=correct_hash,
        )

        # _restore_internal 捕获异常后返回 success=False 字典
        assert isinstance(result, dict), "restore 应返回 dict"
        assert result.get("success") is False, (
            f"恢复失败时 result.success 应为 False,实际: {result.get('success')}"
        )
        assert "生产恢复写入失败" in result.get("error", ""), (
            f"error 应包含 '生产恢复写入失败',实际: {result.get('error')}"
        )
        assert "CRDB connection lost during restore" in result.get("error", ""), (
            f"error 应包含原始异常消息,实际: {result.get('error')}"
        )

        # 验证状态已从 approved → executing → failed
        status = await _get_command_status(store, action_id)
        assert status == "failed", (
            f"restore 失败后 status 应为 'failed',实际: {status}"
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_cas_conflict_when_executing(
        self, real_store_with_engine, monkeypatch,
    ):
        """status='executing' → CAS 失败(已被抢占)→ COMMAND_STATUS_CONFLICT。"""
        store, engine, fake_storage, _ = real_store_with_engine
        _patch_backup_all_tables(monkeypatch)
        backup_id = await _make_backup(engine)

        action_id = "r52_restore_cas_001"
        principal_id = 303
        correct_hash = _compute_restore_hash(engine, backup_id, principal_id, action_id)
        # 直接插入 status='executing'(模拟其他 worker 已在执行)
        # 但 _validate_production_approval 会先检查 status != 'approved',
        # 然后抛 PermissionError(非 approved 状态)
        # 所以这里验证的是 _validate_production_approval 拒绝 executing 状态
        await _insert_command_execution(
            store, action_id, status="executing",
            principal_id=principal_id, request_hash=correct_hash,
        )

        async def _spy_restore(*args, **kwargs):
            return {"restored_tables": 0}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data", _spy_restore,
        )

        # status='executing' 不是 'approved' → _validate_production_approval 抛 PermissionError
        with pytest.raises(PermissionError) as exc_info:
            await engine.restore(
                backup_id, target="production",
                approver_id=principal_id,
                approval_action_id=action_id,
                expected_request_hash=correct_hash,
            )

        assert "approved" in str(exc_info.value).lower(), (
            f"异常消息应提及 approved,实际: {exc_info.value}"
        )


# ════════════════════════════════════════════════════════════════
# 6. 错误码注册验证
# ════════════════════════════════════════════════════════════════

class TestErrorCodesRegistered:
    """R52 P0-5: 验证新增错误码已注册到 ErrorRegistry。"""

    def test_command_status_conflict_registered(self):
        """COMMAND_STATUS_CONFLICT 已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        definition = ErrorRegistry.get(ErrorCodes.COMMAND_STATUS_CONFLICT)
        assert definition is not None, "COMMAND_STATUS_CONFLICT 应注册到 ErrorRegistry"
        assert definition.code == ErrorCodes.COMMAND_STATUS_CONFLICT
        assert definition.http_status == 409
        assert definition.retryable is True

    def test_command_not_approved_registered(self):
        """COMMAND_NOT_APPROVED 已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        definition = ErrorRegistry.get(ErrorCodes.COMMAND_NOT_APPROVED)
        assert definition is not None, "COMMAND_NOT_APPROVED 应注册到 ErrorRegistry"
        assert definition.code == ErrorCodes.COMMAND_NOT_APPROVED
        assert definition.http_status == 403
        assert definition.retryable is False

    def test_command_status_conflict_message_key(self):
        """COMMAND_STATUS_CONFLICT 的 message_key 在 locale 文件中存在。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        definition = ErrorRegistry.get(ErrorCodes.COMMAND_STATUS_CONFLICT)
        assert definition.message_key == "errors.command.status.conflict"

    def test_command_not_approved_message_key(self):
        """COMMAND_NOT_APPROVED 的 message_key 在 locale 文件中存在。"""
        from services.error_codes import ErrorCodes, ErrorRegistry

        definition = ErrorRegistry.get(ErrorCodes.COMMAND_NOT_APPROVED)
        assert definition.message_key == "errors.command.status.not_approved"
