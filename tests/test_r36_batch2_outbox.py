"""R36 Batch 2: B0-2 Upload Outbox Worker 独占执行测试。

被测目标:
- ``database.cache_store.CacheStore`` 的 ``upload_outbox`` 表新增 lease 字段:
  ``lease_owner`` / ``lease_until``(支持 OutboxWorker CAS claim)
- ``database.cache_store.CacheStore.claim_outbox_entry`` CAS claim 方法
- ``database.cache_store.CacheStore.mark_outbox_failed`` 支持 max_attempts → DEAD
- ``database.cache_store.CacheStore.mark_outbox_dead`` 直接置 DEAD
- ``database.cache_store.CacheStore.reset_stale_outbox`` 清理 lease 过期的 DISPATCHED
- ``services.outbox_worker.OutboxWorker`` 类:
  - 主循环扫描 PENDING + CAS claim + dispatch + mark_done/failed
  - REGISTER_MANIFEST / ARCHIVE_R100 / UPLOAD_FAILED 事件分派
  - 指数退避 + max_attempts → DEAD
  - 幂等保证(outbox_id + claim CAS + upsert_manifest)
  - 信号处理 / 优雅停止

测试策略:
- 使用真实 SQLite 临时文件数据库,验证 DDL 升级幂等性
- 使用 mock 回调函数,验证 OutboxWorker 的分派逻辑
- 验证幂等性:同一 outbox_id 多次处理不重复执行副作用
- 验证 lease 机制:被其他 worker 抢占后跳过

对应 R36 B0-2 要求:
- copy 后 → upload_outbox 持久化 → OutboxWorker claim → 执行副作用 → DONE/DEAD
- 移除主链路 create_task 作为唯一执行机制
- READY = Manifest 已持久化;R100 不阻塞主取件
- outbox claim 具备 lease/attempts/next_retry_at/event_id
"""
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore

# 尝试导入 OutboxWorker(依赖 loguru)
_outbox_worker_available = False
try:
    from services.outbox_worker import OutboxWorker
    _outbox_worker_available = True
except Exception:
    _outbox_worker_available = False


# ── Fixture: 真实 SQLite 临时数据库 ──────────────────────────────

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r36_batch2_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 1. upload_outbox 表结构升级测试(DDL 幂等性)
# ════════════════════════════════════════════════════════════════

class TestUploadOutboxSchema:
    """R36 B0-2: upload_outbox 表新增 lease_owner / lease_until 列。"""

    @pytest.mark.asyncio
    async def test_lease_columns_exist_after_init(self, real_store):
        """init() 后 upload_outbox 表应包含 lease_owner / lease_until 列。"""
        rows = await real_store._db.execute_fetchall(
            "PRAGMA table_info(upload_outbox)"
        )
        column_names = {r[1] for r in rows}
        assert "lease_owner" in column_names, "缺少 lease_owner 列"
        assert "lease_until" in column_names, "缺少 lease_until 列"

    @pytest.mark.asyncio
    async def test_default_values_for_lease_columns(self, real_store):
        """新列默认值:lease_owner=NULL, lease_until=NULL。"""
        # 插入一条 outbox(create_outbox_entry 不传 lease 字段)
        await real_store.create_outbox_entry(
            "obx-test-001", "upload-001", "C1", 1001, 2001,
            storage_msg_ids=[5001], batch_file_meta=[],
            event_type="REGISTER_MANIFEST",
        )
        rows = await real_store._db.execute_fetchall(
            "SELECT lease_owner, lease_until FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-test-001",),
        )
        assert rows, "查询应返回一行"
        lease_owner, lease_until = rows[0]
        assert lease_owner is None, f"lease_owner 默认应为 NULL,实际: {lease_owner}"
        assert lease_until is None, f"lease_until 默认应为 NULL,实际: {lease_until}"

    @pytest.mark.asyncio
    async def test_init_idempotent_with_repeated_alter(self, real_store):
        """多次 init() 不应因 ALTER TABLE 重复列错误失败。"""
        await real_store.init()  # 第二次 init
        # 验证表仍可正常插入
        await real_store.create_outbox_entry(
            "obx-idempotent-001", "upload-002", "C2", 1002, 2002,
            event_type="REGISTER_MANIFEST",
        )
        rows = await real_store._db.execute_fetchall(
            "SELECT outbox_id FROM upload_outbox WHERE outbox_id = ?",
            ("obx-idempotent-001",),
        )
        assert rows, "幂等 init 后应能正常写入"


# ════════════════════════════════════════════════════════════════
# 2. claim_outbox_entry CAS 机制测试
# ════════════════════════════════════════════════════════════════

class TestClaimOutboxEntry:
    """R36 B0-2: claim_outbox_entry 通过 CAS 获取独占执行权。"""

    @pytest.mark.asyncio
    async def test_claim_pending_succeeds(self, real_store):
        """PENDING 状态的 outbox 可被 claim。"""
        await real_store.create_outbox_entry(
            "obx-claim-001", "upload-001", "C1", 1001, 2001,
            event_type="REGISTER_MANIFEST",
        )
        claimed = await real_store.claim_outbox_entry(
            "obx-claim-001", "worker-A", lease_seconds=60,
        )
        assert claimed is True
        # 验证状态已变更为 DISPATCHED
        rows = await real_store._db.execute_fetchall(
            "SELECT status, lease_owner FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-claim-001",),
        )
        assert rows[0][0] == "DISPATCHED"
        assert rows[0][1] == "worker-A"

    @pytest.mark.asyncio
    async def test_claim_dispathed_by_other_fails(self, real_store):
        """已被其他 worker claim(DISPATCHED)的 outbox 无法再次 claim。"""
        await real_store.create_outbox_entry(
            "obx-claim-002", "upload-002", "C2", 1002, 2002,
            event_type="REGISTER_MANIFEST",
        )
        # worker-A claim
        first = await real_store.claim_outbox_entry(
            "obx-claim-002", "worker-A", lease_seconds=60,
        )
        assert first is True
        # worker-B 尝试 claim 同一条
        second = await real_store.claim_outbox_entry(
            "obx-claim-002", "worker-B", lease_seconds=60,
        )
        assert second is False, "已被其他 worker claim 的 outbox 不应被再次 claim"

    @pytest.mark.asyncio
    async def test_claim_after_done_fails(self, real_store):
        """DONE 状态的 outbox 无法 claim(终态)。"""
        await real_store.create_outbox_entry(
            "obx-claim-003", "upload-003", "C3", 1003, 2003,
            event_type="REGISTER_MANIFEST",
        )
        await real_store.mark_outbox_done("obx-claim-003")
        claimed = await real_store.claim_outbox_entry(
            "obx-claim-003", "worker-A",
        )
        assert claimed is False, "DONE 状态的 outbox 不应被 claim"

    @pytest.mark.asyncio
    async def test_claim_after_dead_fails(self, real_store):
        """DEAD 状态的 outbox 无法 claim(终态)。"""
        await real_store.create_outbox_entry(
            "obx-claim-004", "upload-004", "C4", 1004, 2004,
            event_type="REGISTER_MANIFEST",
        )
        await real_store.mark_outbox_dead("obx-claim-004", reason="test_dead")
        claimed = await real_store.claim_outbox_entry(
            "obx-claim-004", "worker-A",
        )
        assert claimed is False, "DEAD 状态的 outbox 不应被 claim"

    @pytest.mark.asyncio
    async def test_claim_unknown_outbox_fails(self, real_store):
        """不存在的 outbox_id claim 应返回 False(不抛异常)。"""
        claimed = await real_store.claim_outbox_entry(
            "obx-nonexistent", "worker-A",
        )
        assert claimed is False


# ════════════════════════════════════════════════════════════════
# 3. mark_outbox_done / mark_outbox_failed / mark_outbox_dead 测试
# ════════════════════════════════════════════════════════════════

class TestOutboxStateTransitions:
    """R36 B0-2: outbox 状态机转换。"""

    @pytest.mark.asyncio
    async def test_mark_done(self, real_store):
        """mark_outbox_done 将 status=DONE。"""
        await real_store.create_outbox_entry(
            "obx-done-001", "upload-001", "C1", 1001, 2001,
            event_type="REGISTER_MANIFEST",
        )
        ok = await real_store.mark_outbox_done("obx-done-001")
        assert ok is True
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-done-001",),
        )
        assert rows[0][0] == "DONE"

    @pytest.mark.asyncio
    async def test_mark_failed_increments_attempts(self, real_store):
        """mark_outbox_failed 将 attempts+1,设置 next_retry_at。"""
        await real_store.create_outbox_entry(
            "obx-fail-001", "upload-002", "C2", 1002, 2002,
            event_type="REGISTER_MANIFEST",
        )
        next_retry = time.time() + 30
        ok = await real_store.mark_outbox_failed(
            "obx-fail-001", "test failure", next_retry,
        )
        assert ok is True
        rows = await real_store._db.execute_fetchall(
            "SELECT status, attempts, next_retry_at FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-fail-001",),
        )
        # mark_outbox_failed 不直接改 status(保持 PENDING 等待下次扫描)
        assert rows[0][0] == "PENDING"
        assert rows[0][1] == 1, f"attempts 应为 1,实际: {rows[0][1]}"
        assert rows[0][2] is not None

    @pytest.mark.asyncio
    async def test_mark_failed_exceeding_max_attempts_becomes_dead(self, real_store):
        """超过 max_attempts 时自动置为 DEAD。"""
        await real_store.create_outbox_entry(
            "obx-fail-dead-001", "upload-003", "C3", 1003, 2003,
            event_type="REGISTER_MANIFEST",
        )
        # 模拟 3 次失败(attempts 从 0 → 3,max_attempts=3)
        next_retry = time.time() + 30
        await real_store.mark_outbox_failed(
            "obx-fail-dead-001", "fail-1", next_retry, max_attempts=3,
        )
        await real_store.mark_outbox_failed(
            "obx-fail-dead-001", "fail-2", next_retry, max_attempts=3,
        )
        await real_store.mark_outbox_failed(
            "obx-fail-dead-001", "fail-3", next_retry, max_attempts=3,
        )
        rows = await real_store._db.execute_fetchall(
            "SELECT status, attempts FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-fail-dead-001",),
        )
        # attempts=3 >= max_attempts=3,应自动置为 DEAD
        assert rows[0][0] == "DEAD", f"超过 max_attempts 应为 DEAD,实际: {rows[0][0]}"
        assert rows[0][1] == 3

    @pytest.mark.asyncio
    async def test_mark_dead_directly(self, real_store):
        """mark_outbox_dead 直接置 DEAD。"""
        await real_store.create_outbox_entry(
            "obx-dead-001", "upload-004", "C4", 1004, 2004,
            event_type="REGISTER_MANIFEST",
        )
        ok = await real_store.mark_outbox_dead("obx-dead-001", reason="manual_dead")
        assert ok is True
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-dead-001",),
        )
        assert rows[0][0] == "DEAD"


# ════════════════════════════════════════════════════════════════
# 4. reset_stale_outbox 测试
# ════════════════════════════════════════════════════════════════

class TestResetStaleOutbox:
    """R36 B0-2: 清理 lease 过期但状态仍为 DISPATCHED 的 outbox 条目。"""

    @pytest.mark.asyncio
    async def test_reset_stale_dispatched_to_pending(self, real_store):
        """lease 过期的 DISPATCHED 条目应被重置为 PENDING。"""
        await real_store.create_outbox_entry(
            "obx-stale-001", "upload-001", "C1", 1001, 2001,
            event_type="REGISTER_MANIFEST",
        )
        # claim 设置 lease=1s
        await real_store.claim_outbox_entry(
            "obx-stale-001", "worker-A", lease_seconds=1,
        )
        # 等 lease 过期
        await asyncio.sleep(1.5)
        # 重置 stale(排除 worker-A 自己,模拟另一个 worker)
        reset_count = await real_store.reset_stale_outbox("worker-B")
        assert reset_count == 1
        rows = await real_store._db.execute_fetchall(
            "SELECT status, lease_owner FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-stale-001",),
        )
        assert rows[0][0] == "PENDING"
        assert rows[0][1] is None

    @pytest.mark.asyncio
    async def test_reset_stale_excludes_self(self, real_store):
        """reset_stale_outbox 不重置自己 owner 名下的 DISPATCHED。"""
        await real_store.create_outbox_entry(
            "obx-stale-002", "upload-002", "C2", 1002, 2002,
            event_type="REGISTER_MANIFEST",
        )
        await real_store.claim_outbox_entry(
            "obx-stale-002", "worker-A", lease_seconds=1,
        )
        await asyncio.sleep(1.5)
        # worker-A 自己 reset,不应重置自己 lease 的条目
        reset_count = await real_store.reset_stale_outbox("worker-A")
        assert reset_count == 0
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-stale-002",),
        )
        assert rows[0][0] == "DISPATCHED"  # 仍为 DISPATCHED

    @pytest.mark.asyncio
    async def test_reset_stale_no_lease_until_skipped(self, real_store):
        """lease_until=NULL 的 DISPATCHED 条目不应被重置(可能是手动置为 DISPATCHED 的特殊场景)。"""
        # 直接 SQL 插入 lease_until=NULL 的 DISPATCHED 条目
        await real_store._db.execute(
            "INSERT INTO upload_outbox "
            "(outbox_id, upload_id, code, target_user_id, "
            "storage_channel_id, event_type, status, attempts, created_at, "
            "lease_owner, lease_until) "
            "VALUES (?, ?, ?, ?, ?, ?, 'DISPATCHED', 0, ?, NULL, NULL)",
            ("obx-stale-003", "upload-003", "C3", 1003, 2003,
             "REGISTER_MANIFEST", time.time()),
        )
        await real_store._db.commit()
        reset_count = await real_store.reset_stale_outbox("worker-X")
        assert reset_count == 0


# ════════════════════════════════════════════════════════════════
# 5. OutboxWorker 完整流程测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _outbox_worker_available,
    reason="services.outbox_worker.OutboxWorker 不可用(需要 loguru)",
)
class TestOutboxWorkerFlow:
    """R36 B0-2: OutboxWorker 完整流程测试。"""

    @pytest.mark.asyncio
    async def test_worker_processes_pending_entry(self, real_store):
        """OutboxWorker 扫描 PENDING → claim → dispatch → DONE。"""
        await real_store.create_outbox_entry(
            "obx-wf-001", "upload-001", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            batch_file_meta=[{
                "type": "photo", "file_id": "fid-1",
                "file_unique_id": "fuid-wf-001",
                "group_id": 1, "media_group_id": "",
            }],
            event_type="REGISTER_MANIFEST",
        )
        register_fn = AsyncMock()
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
            lease_seconds=60,
        )
        await worker.start()
        # 等待 worker 处理
        await asyncio.sleep(0.3)
        await worker.stop()
        # 验证回调被调用
        register_fn.assert_awaited_once()
        # 验证参数:channel_id, msg_id, file_meta dict
        call_args = register_fn.await_args
        assert call_args.args[0] == 2001  # storage_channel_id
        assert call_args.args[1] == 5001  # storage_msg_id
        assert call_args.args[2]["file_unique_id"] == "fuid-wf-001"
        # 验证状态变为 DONE
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-wf-001",),
        )
        assert rows[0][0] == "DONE"

    @pytest.mark.asyncio
    async def test_worker_dispatches_archive_r100(self, real_store):
        """OutboxWorker 正确分派 ARCHIVE_R100 事件。"""
        await real_store.create_outbox_entry(
            "obx-wf-r100-001", "upload-r100", "C1", 1001, 2001,
            storage_msg_ids=[5001, 5002],
            event_type="ARCHIVE_R100",
        )
        archive_fn = AsyncMock()
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=AsyncMock(),
            archive_to_r100_fn=archive_fn,
            scan_interval=0.05,
        )
        await worker.start()
        await asyncio.sleep(0.3)
        await worker.stop()
        # 每个 msg_id 应调用一次 archive_fn(共 2 次)
        assert archive_fn.await_count == 2
        # 验证参数
        first_call = archive_fn.await_args_list[0]
        assert first_call.args[0] == 2001  # storage_channel_id
        assert first_call.args[1] == 5001  # first msg_id
        second_call = archive_fn.await_args_list[1]
        assert second_call.args[1] == 5002

    @pytest.mark.asyncio
    async def test_worker_failure_triggers_retry(self, real_store):
        """回调抛异常时,outbox 进入 attempts+1 + next_retry_at(等待重试)。"""
        await real_store.create_outbox_entry(
            "obx-wf-fail-001", "upload-fail", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            event_type="ARCHIVE_R100",
        )
        archive_fn = AsyncMock(side_effect=RuntimeError("simulated failure"))
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=AsyncMock(),
            archive_to_r100_fn=archive_fn,
            scan_interval=0.05,
            max_attempts=3,
            backoff_base=0.1,  # 短退避便于测试
        )
        await worker.start()
        await asyncio.sleep(0.3)
        await worker.stop()
        # 验证 attempts 增加
        rows = await real_store._db.execute_fetchall(
            "SELECT status, attempts, next_retry_at FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-wf-fail-001",),
        )
        assert rows[0][1] >= 1, f"attempts 应 >= 1,实际: {rows[0][1]}"
        assert rows[0][2] is not None, "next_retry_at 应非 NULL"

    @pytest.mark.asyncio
    async def test_worker_max_attempts_becomes_dead(self, real_store):
        """失败次数超过 max_attempts 时,outbox 自动置为 DEAD。"""
        await real_store.create_outbox_entry(
            "obx-wf-dead-001", "upload-dead", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            event_type="ARCHIVE_R100",
        )
        archive_fn = AsyncMock(side_effect=RuntimeError("always fails"))
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=AsyncMock(),
            archive_to_r100_fn=archive_fn,
            scan_interval=0.05,
            max_attempts=2,
            backoff_base=0.05,
            backoff_max=0.1,
        )
        await worker.start()
        # 等待足够时间让 worker 重试 2 次后置 DEAD
        await asyncio.sleep(1.0)
        await worker.stop()
        rows = await real_store._db.execute_fetchall(
            "SELECT status, attempts FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-wf-dead-001",),
        )
        assert rows[0][0] == "DEAD", f"超过 max_attempts 应为 DEAD,实际: {rows[0][0]}"
        assert rows[0][1] >= 2

    @pytest.mark.asyncio
    async def test_worker_idempotent_no_duplicate_dispatch(self, real_store):
        """幂等性:同一 outbox_id 多次扫描只执行一次副作用(CAS claim 保证)。"""
        await real_store.create_outbox_entry(
            "obx-wf-idem-001", "upload-idem", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            event_type="REGISTER_MANIFEST",
        )
        register_fn = AsyncMock()
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
        )
        await worker.start()
        await asyncio.sleep(0.5)
        await worker.stop()
        # 即使 worker 多次扫描,register_fn 应只调用一次(claim CAS 保证)
        assert register_fn.await_count == 1, (
            f"幂等保证:register_fn 应只调用 1 次,实际 {register_fn.await_count}"
        )

    @pytest.mark.asyncio
    async def test_worker_two_workers_no_duplicate(self, real_store):
        """两个 worker 并发处理同一 outbox,只有一方成功 claim(无重复副作用)。"""
        await real_store.create_outbox_entry(
            "obx-wf-2w-001", "upload-2w", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            event_type="REGISTER_MANIFEST",
        )
        register_fn_a = AsyncMock()
        register_fn_b = AsyncMock()
        worker_a = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn_a,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
            owner="worker-A-test",
        )
        worker_b = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn_b,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
            owner="worker-B-test",
        )
        await worker_a.start()
        await worker_b.start()
        await asyncio.sleep(0.5)
        await worker_a.stop()
        await worker_b.stop()
        # 只有一个 worker 的回调应被调用(总和=1)
        total_calls = register_fn_a.await_count + register_fn_b.await_count
        assert total_calls == 1, (
            f"两 worker 并发应只调用 1 次,实际 {total_calls} "
            f"(A={register_fn_a.await_count}, B={register_fn_b.await_count})"
        )

    @pytest.mark.asyncio
    async def test_worker_lease_recovery(self, real_store):
        """lease 恢复:worker A 崩溃后 lease 过期,worker B 可重新 claim。"""
        await real_store.create_outbox_entry(
            "obx-wf-recover-001", "upload-recover", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            event_type="REGISTER_MANIFEST",
        )
        # worker-A claim 后立即停止(模拟崩溃,未 mark_done)
        await real_store.claim_outbox_entry(
            "obx-wf-recover-001", "worker-A-recover", lease_seconds=1,
        )
        # 等 lease 过期
        await asyncio.sleep(1.5)
        # worker-B 启动,reset_stale_outbox 应将条目重置为 PENDING,然后被处理
        register_fn = AsyncMock()
        worker_b = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
            owner="worker-B-recover",
        )
        await worker_b.start()
        await asyncio.sleep(0.3)
        await worker_b.stop()
        # worker-B 应能处理(经过 reset_stale_outbox 恢复)
        assert register_fn.await_count == 1
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-wf-recover-001",),
        )
        assert rows[0][0] == "DONE"

    @pytest.mark.asyncio
    async def test_worker_start_stop_lifecycle(self, real_store):
        """OutboxWorker start/stop 生命周期正常。"""
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=AsyncMock(),
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.1,
        )
        assert worker.is_running is False
        await worker.start()
        assert worker.is_running is True
        await worker.stop()
        assert worker.is_running is False

    @pytest.mark.asyncio
    async def test_worker_unknown_event_type_completes(self, real_store):
        """未知 event_type 视为完成(避免 DEAD 卡住)。"""
        await real_store.create_outbox_entry(
            "obx-wf-unknown-001", "upload-unknown", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            event_type="UNKNOWN_EVENT_TYPE",
        )
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=AsyncMock(),
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
        )
        await worker.start()
        await asyncio.sleep(0.3)
        await worker.stop()
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-wf-unknown-001",),
        )
        assert rows[0][0] == "DONE"


# ════════════════════════════════════════════════════════════════
# 6. OutboxWorker 集成测试:up_bot 回调接入(若可导入)
# ════════════════════════════════════════════════════════════════

_up_bot_available = False
try:
    import bots.up_bot as up_bot_module
    _up_bot_available = True
except Exception:
    _up_bot_available = False


@pytest.mark.skipif(
    not _up_bot_available or not _outbox_worker_available,
    reason="bots.up_bot 或 services.outbox_worker 不可用",
)
class TestOutboxWorkerUpBotIntegration:
    """R36 B0-2: 验证 up_bot 中已无 create_task fire-and-forget 调用。"""

    def test_no_create_task_register_manifest(self):
        """up_bot 中应无 asyncio.create_task(_register_manifest(...)) 调用。"""
        import inspect as _inspect
        source = _inspect.getsource(up_bot_module)
        assert "asyncio.create_task(_register_manifest" not in source, (
            "up_bot 仍包含 asyncio.create_task(_register_manifest(...)) "
            "fire-and-forget 调用,违反 R36 B0-2"
        )

    def test_no_create_task_forward_to_r100(self):
        """up_bot 中应无 asyncio.create_task(_forward_to_r100(...)) 调用。"""
        import inspect as _inspect
        source = _inspect.getsource(up_bot_module)
        assert "asyncio.create_task(_forward_to_r100" not in source, (
            "up_bot 仍包含 asyncio.create_task(_forward_to_r100(...)) "
            "fire-and-forget 调用,违反 R36 B0-2"
        )

    def test_register_manifest_function_preserved(self):
        """_register_manifest 函数应保留(被 OutboxWorker 间接调用)。"""
        assert hasattr(up_bot_module, "_register_manifest")
        assert hasattr(up_bot_module, "_forward_to_r100")
        # R36 B0-2 新增的 strict wrapper
        assert hasattr(up_bot_module, "_outbox_register_manifest_strict")
        assert hasattr(up_bot_module, "_outbox_archive_to_r100_strict")

    @pytest.mark.asyncio
    async def test_strict_register_manifest_uses_upsert(self, real_store):
        """_outbox_register_manifest_strict 调用 upsert_manifest(幂等)。"""
        # 先在 manifest 表插入一条(模拟已存在)
        await real_store.upsert_manifest(
            group_id=1, file_unique_id="fuid-strict-001",
            channel_id=2001, message_id=5001,
            media_type="photo", media_group_id="",
        )
        # 调用 strict wrapper(应幂等更新,不报错)
        # 先 mock 全局 _bot 和 _channel_to_group
        with patch.object(up_bot_module, "_channel_to_group", {2001: 1}):
            with patch.object(up_bot_module, "_channel_to_group_ts", time.time()):
                with patch.object(up_bot_module, "get_cache_store", return_value=real_store):
                    # file_meta 包含 file_unique_id
                    await up_bot_module._outbox_register_manifest_strict(
                        channel_id=2001, message_id=5001,
                        file_meta={
                            "type": "photo", "file_id": "fid-1",
                            "file_unique_id": "fuid-strict-001",
                            "group_id": 1, "media_group_id": "",
                        },
                    )
        # 验证 manifest 表中记录存在(更新而非新增)
        rows = await real_store._db.execute_fetchall(
            "SELECT message_id FROM manifest "
            "WHERE group_id = ? AND file_unique_id = ?",
            (1, "fuid-strict-001"),
        )
        assert rows, "manifest 表应有记录"
        assert rows[0][0] == 5001

    @pytest.mark.asyncio
    async def test_strict_register_manifest_raises_on_missing_group(self, real_store):
        """_outbox_register_manifest_strict 在 group_id 未映射时抛异常(触发重试)。"""
        with patch.object(up_bot_module, "_channel_to_group", {}):
            with patch.object(up_bot_module, "_channel_to_group_ts", time.time()):
                with pytest.raises(RuntimeError, match="无法解析频道"):
                    await up_bot_module._outbox_register_manifest_strict(
                        channel_id=9999, message_id=9999,
                        file_meta={"file_unique_id": "fuid-test"},
                    )


# ════════════════════════════════════════════════════════════════
# 7. R36 B0-2 验收场景模拟:kill -9 后重启恢复
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _outbox_worker_available,
    reason="OutboxWorker 不可用",
)
class TestKillRecoveryScenarios:
    """R36 B0-2 验收: copy 后、outbox 前、worker 执行中 kill -9,重启后最终一致。"""

    @pytest.mark.asyncio
    async def test_kill_after_copy_before_worker(self, real_store):
        """模拟: copy 完成、outbox 已写入 PENDING,worker 尚未启动 → 重启后能完整处理。"""
        # 模拟 _finalize_upload 写入 outbox(copy 已完成)
        await real_store.create_outbox_entry(
            "obx-kill-001", "upload-kill-001", "C1", 1001, 2001,
            storage_msg_ids=[5001, 5002],
            batch_file_meta=[
                {"type": "photo", "file_unique_id": "fuid-kill-001",
                 "group_id": 1, "media_group_id": ""},
                {"type": "video", "file_unique_id": "fuid-kill-002",
                 "group_id": 1, "media_group_id": ""},
            ],
            event_type="REGISTER_MANIFEST",
        )
        # worker 启动后应处理这两个 msg_id
        register_fn = AsyncMock()
        worker = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
        )
        await worker.start()
        await asyncio.sleep(0.5)
        await worker.stop()
        # 验证两个文件都被 manifest 注册
        assert register_fn.await_count == 2
        # 最终状态 DONE
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-kill-001",),
        )
        assert rows[0][0] == "DONE"

    @pytest.mark.asyncio
    async def test_kill_during_worker_processing(self, real_store):
        """模拟: worker claim 后 mark_done 前 kill -9 → 重启后 lease 过期可重新处理。"""
        await real_store.create_outbox_entry(
            "obx-kill-002", "upload-kill-002", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            batch_file_meta=[{
                "type": "photo", "file_unique_id": "fuid-kill-002",
                "group_id": 1, "media_group_id": "",
            }],
            event_type="REGISTER_MANIFEST",
        )
        # 模拟 worker-A claim 后崩溃(未 mark_done)
        await real_store.claim_outbox_entry(
            "obx-kill-002", "worker-A-crashed", lease_seconds=1,
        )
        # 等 lease 过期
        await asyncio.sleep(1.5)
        # worker-B 启动,reset_stale_outbox 恢复条目为 PENDING,然后处理
        register_fn = AsyncMock()
        worker_b = OutboxWorker(
            store=real_store,
            register_manifest_fn=register_fn,
            archive_to_r100_fn=AsyncMock(),
            scan_interval=0.05,
            owner="worker-B-recovery",
        )
        await worker_b.start()
        await asyncio.sleep(0.5)
        await worker_b.stop()
        # 验证 register_fn 被调用一次(无重复)
        assert register_fn.await_count == 1, (
            f"恢复后应只调用 1 次,实际 {register_fn.await_count}"
        )
        # 最终状态 DONE
        rows = await real_store._db.execute_fetchall(
            "SELECT status FROM upload_outbox WHERE outbox_id = ?",
            ("obx-kill-002",),
        )
        assert rows[0][0] == "DONE"

    @pytest.mark.asyncio
    async def test_no_duplicate_manifest_after_retry(self, real_store):
        """验收: 重试后最终 Manifest 完整、无重复登记。"""
        # 写入 outbox,首次失败(模拟 register_manifest 第一次失败)
        # 然后第二次成功。验证 manifest 表中只有一条记录(无重复)
        await real_store.create_outbox_entry(
            "obx-kill-003", "upload-kill-003", "C1", 1001, 2001,
            storage_msg_ids=[5001],
            batch_file_meta=[{
                "type": "photo", "file_unique_id": "fuid-kill-003",
                "group_id": 1, "media_group_id": "",
            }],
            event_type="REGISTER_MANIFEST",
        )
        # 直接验证 upsert_manifest 幂等性(重复 INSERT OR REPLACE 不创建重复)
        await real_store.upsert_manifest(
            group_id=1, file_unique_id="fuid-kill-003",
            channel_id=2001, message_id=5001,
            media_type="photo",
        )
        # 模拟重试:第二次 upsert_manifest(更新 message_id)
        await real_store.upsert_manifest(
            group_id=1, file_unique_id="fuid-kill-003",
            channel_id=2001, message_id=5001,
            media_type="photo",
        )
        # 验证 manifest 表只有一条记录(无重复)
        rows = await real_store._db.execute_fetchall(
            "SELECT COUNT(*) FROM manifest "
            "WHERE group_id = ? AND file_unique_id = ?",
            (1, "fuid-kill-003"),
        )
        assert rows[0][0] == 1, f"重复 upsert 后应只有 1 条记录,实际: {rows[0][0]}"


# ════════════════════════════════════════════════════════════════
# 8. lease 与 attempts 字段持久化测试
# ════════════════════════════════════════════════════════════════

class TestLeaseAndAttemptsPersistence:
    """R36 B0-2: outbox claim 需 lease/attempts/next_retry_at/event_id 持久化。"""

    @pytest.mark.asyncio
    async def test_claim_persists_lease_owner_and_until(self, real_store):
        """claim 成功后,lease_owner 和 lease_until 应持久化到表中。"""
        await real_store.create_outbox_entry(
            "obx-lease-001", "upload-001", "C1", 1001, 2001,
            event_type="REGISTER_MANIFEST",
        )
        await real_store.claim_outbox_entry(
            "obx-lease-001", "worker-lease-test", lease_seconds=60,
        )
        rows = await real_store._db.execute_fetchall(
            "SELECT lease_owner, lease_until FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-lease-001",),
        )
        assert rows[0][0] == "worker-lease-test"
        assert rows[0][1] is not None
        # lease_until 应在未来
        assert rows[0][1] > time.time()

    @pytest.mark.asyncio
    async def test_failed_persists_attempts_and_next_retry(self, real_store):
        """失败后 attempts 和 next_retry_at 应持久化。"""
        await real_store.create_outbox_entry(
            "obx-att-001", "upload-002", "C2", 1002, 2002,
            event_type="REGISTER_MANIFEST",
        )
        next_retry = time.time() + 60
        await real_store.mark_outbox_failed(
            "obx-att-001", "test failure", next_retry,
        )
        rows = await real_store._db.execute_fetchall(
            "SELECT attempts, next_retry_at FROM upload_outbox "
            "WHERE outbox_id = ?",
            ("obx-att-001",),
        )
        assert rows[0][0] == 1, f"attempts 应为 1,实际: {rows[0][0]}"
        assert abs(rows[0][1] - next_retry) < 1.0, (
            f"next_retry_at 应接近 {next_retry},实际 {rows[0][1]}"
        )

    @pytest.mark.asyncio
    async def test_event_id_persistence(self, real_store):
        """event_type 字段持久化(作为 event_id 标识不同事件)。"""
        await real_store.create_outbox_entry(
            "obx-event-001", "upload-001", "C1", 1001, 2001,
            event_type="REGISTER_MANIFEST",
        )
        await real_store.create_outbox_entry(
            "obx-event-002", "upload-001", "C1", 1001, 2001,
            event_type="ARCHIVE_R100",
        )
        # 通过 event_type 区分不同事件
        rows = await real_store._db.execute_fetchall(
            "SELECT outbox_id, event_type FROM upload_outbox "
            "WHERE upload_id = ? ORDER BY outbox_id",
            ("upload-001",),
        )
        assert len(rows) == 2
        event_types = {r[1] for r in rows}
        assert "REGISTER_MANIFEST" in event_types
        assert "ARCHIVE_R100" in event_types

    @pytest.mark.asyncio
    async def test_outbox_id_is_idempotent_key(self, real_store):
        """outbox_id 作为幂等键:重复 create 不应创建多条(INSERT OR IGNORE)。"""
        for _ in range(3):
            await real_store.create_outbox_entry(
                "obx-idem-001", "upload-001", "C1", 1001, 2001,
                event_type="REGISTER_MANIFEST",
            )
        rows = await real_store._db.execute_fetchall(
            "SELECT COUNT(*) FROM upload_outbox WHERE outbox_id = ?",
            ("obx-idem-001",),
        )
        assert rows[0][0] == 1, (
            f"重复 create 同一 outbox_id 应只插入 1 条(INSERT OR IGNORE),"
            f"实际: {rows[0][0]}"
        )
