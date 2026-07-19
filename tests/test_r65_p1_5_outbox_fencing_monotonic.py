"""R65 P1-05: outbox fencing lease_version 单调递增 + 严格 CAS 冲突 raise 整改测试。

被测目标(R65 P1-05 整改要求):
- ``database.cache_store.CacheStore.claim_outbox_events`` —— fencing token 单调递增:
    * CAS 条件 ``WHERE status='pending'``(不再要求 lease_version=0)
    * 成功后 ``lease_version = lease_version + 1``(单调递增,永不重置)
- ``database.cache_store.CacheStore.reclaim_stale_outbox_leases`` —— 不重置版本:
    * 仅清 status / lease_owner / lease_expires_at
    * ``lease_version`` 保持当前值(消除 ABA 风险)
- ``database.cache_store.CacheStore.fail_outbox_event`` —— retryable 路径不重置版本:
    * retryable 路径仅清 lease_owner / lease_expires_at / status='pending'
    * ``lease_version`` 保持当前值(消除 ABA 风险)
- ``database.cache_store.CacheStore.complete_outbox_event`` / ``fail_outbox_event`` /
  ``renew_outbox_lease`` —— 严格 CAS 冲突 raise:
    * ``lease_version is not None`` 路径下 CAS 失败(行未找到 / 版本不匹配 /
      lease_owner 不匹配 / request_hash 不匹配)一律 raise
      ``AppError(OUTBOX_LEASE_VERSION_CONFLICT)``,不再静默返回
      False/not_found
- ``services.data_lifecycle.OutboxWorker.__init__`` —— ``test_mode`` 参数彻底移除:
    * 传入 ``test_mode=...`` 即 TypeError
    * 测试必须注入独立 fake provider,不再依赖 stub 分支
- ``services.data_lifecycle.OutboxWorker._persist_and_invoke_compensation``
  —— 补偿持久化:
    * provider 失败且进入 DLQ 时,补偿意图持久化到 audit_log
      (kind='compensation'),保证 worker 重启后 reconcile 流程仍可重放补偿
    * Python 补偿回调作为实现细节,持久化意图是真相源(single source of truth)

测试覆盖(9 项):
1. claim_outbox_events 递增 lease_version(0 → 1)
2. reclaim_stale_outbox_leases 保留 lease_version(不重置为 0)
3. complete_outbox_event 错误 lease_version → raise AppError(OUTBOX_LEASE_VERSION_CONFLICT)
4. fail_outbox_event 错误 lease_version → raise AppError(OUTBOX_LEASE_VERSION_CONFLICT)
5. renew_outbox_lease 递增 lease_version(1 → 2)+ 错误版本 raise
6. lease_version 在 5 次 claim/fail/reclaim 循环中单调递增(永不重置)
7. test_mode 参数已移除 → TypeError
8. 补偿持久化:DLQ 时写入 audit_log(kind='compensation'),重启后可重放
9. complete/fail/renew 严格 CAS 冲突 raise 的错误码与 params 一致
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from services.error_codes import AppError, ErrorCodes

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


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(由 init() 创建含 R65 P1-05 约束的表)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建 outbox_events 表(含 R64 P0-04 / R65 P1-05
    lease_version / dlq_reason / dlq_at 列 + outbox_dlq_audit 审计表 +
    audit_log 审计日志表)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r65_p1_5_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s  # 让 get_cache_store() 返回测试 store
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_receipt_manager_singleton():
    """每个用例前重置 EffectReceiptManager 单例,避免跨用例污染。"""
    from services import effect_receipts as er_mod
    er_mod._receipt_manager = None
    yield
    er_mod._receipt_manager = None


# ════════════════════════════════════════════════════════════════
# 1. claim_outbox_events 递增 lease_version(0 → 1)
# ════════════════════════════════════════════════════════════════

class TestClaimIncrementsLeaseVersion:
    """R65 P1-05: claim_outbox_events 使用 lease_version = lease_version + 1。"""

    @pytest.mark.asyncio
    async def test_claim_increments_lease_version_from_0_to_1(self, real_store):
        """首次 claim: lease_version 0 → 1(单调递增,非固定值 1)。"""
        rh = "rh_r65_claim" + "0" * 53
        eid = await real_store.add_outbox_event(
            action_id="act_r65_claim",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        # 新事件 lease_version=0
        cursor = await real_store._db.execute(
            "SELECT lease_version FROM outbox_events WHERE id=?", (eid,),
        )
        assert (await cursor.fetchone())[0] == 0
        # claim → lease_version = 0 + 1 = 1
        events = await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        assert len(events) == 1
        assert events[0]["lease_version"] == 1
        # 数据库中 lease_version=1
        cursor = await real_store._db.execute(
            "SELECT lease_version, status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1
        assert row[1] == "in_flight"


# ════════════════════════════════════════════════════════════════
# 2. reclaim_stale_outbox_leases 保留 lease_version(不重置为 0)
# ════════════════════════════════════════════════════════════════

class TestReclaimPreservesLeaseVersion:
    """R65 P1-05: reclaim 不重置 lease_version(消除 ABA 风险)。

    旧 R64 实现 ``SET lease_version=0`` 把 fencing token 重置为 0,导致 reclaim 后
    新 claimant 重新拿到 lease_version=1,与之前已用过的 v1 完全相同 → 残留的旧
    worker 调用 complete(v1) 可误完成新 lease 持有者的事件(ABA)。
    新实现:reclaim 仅清 status/owner/lease_expires_at,lease_version 保持当前值;
    新 claimant 通过 lease_version + 1 单调递增 fencing token(避免 ABA)。
    """

    @pytest.mark.asyncio
    async def test_reclaim_preserves_lease_version_not_reset_to_zero(
        self, real_store,
    ):
        """reclaim 后 lease_version 保持当前值(不重置为 0)。"""
        rh = "rh_r65_reclaim" + "0" * 51
        eid = await real_store.add_outbox_event(
            action_id="act_r65_reclaim",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        # claim → lease_version=1
        await real_store.claim_outbox_events(
            lease_owner="worker_dead", lease_duration_seconds=60, limit=1,
        )
        cursor = await real_store._db.execute(
            "SELECT lease_version FROM outbox_events WHERE id=?", (eid,),
        )
        assert (await cursor.fetchone())[0] == 1
        # 模拟 worker 崩溃:手动把 lease_expires_at 设为过去时间
        past_iso = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        await real_store._db.execute(
            "UPDATE outbox_events SET lease_expires_at=? WHERE id=?",
            (past_iso, eid),
        )
        await real_store._db.commit()
        # reclaim
        reclaimed = await real_store.reclaim_stale_outbox_leases(batch_size=10)
        assert reclaimed == 1
        # R65 P1-05: lease_version 保持 1(不重置为 0)
        cursor = await real_store._db.execute(
            "SELECT lease_version, status, lease_owner, lease_expires_at "
            "FROM outbox_events WHERE id=?",
            (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1  # 保留 lease_version(关键不变量)
        assert row[1] == "pending"
        assert row[2] is None  # lease_owner 已清空
        assert row[3] is None  # lease_expires_at 已清空

    @pytest.mark.asyncio
    async def test_reclaim_then_claim_increments_to_2(self, real_store):
        """reclaim 后新 claim → lease_version 1 → 2(单调递增,非重用 v1)。"""
        rh = "rh_r65_reclaim_claim" + "0" * 48
        eid = await real_store.add_outbox_event(
            action_id="act_r65_reclaim_claim",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        # 第一次 claim → lease_version=1
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # 模拟 lease 过期 + reclaim
        past_iso = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        await real_store._db.execute(
            "UPDATE outbox_events SET lease_expires_at=? WHERE id=?",
            (past_iso, eid),
        )
        await real_store._db.commit()
        await real_store.reclaim_stale_outbox_leases(batch_size=10)
        # 第二次 claim → lease_version = 1 + 1 = 2(不重用 v1)
        events = await real_store.claim_outbox_events(
            lease_owner="worker_B", lease_duration_seconds=60, limit=1,
        )
        assert len(events) == 1
        assert events[0]["lease_version"] == 2  # 关键:递增到 2(非重用 1)


# ════════════════════════════════════════════════════════════════
# 3. complete_outbox_event 错误 lease_version → raise AppError
# ════════════════════════════════════════════════════════════════

class TestCompleteWrongVersionRaisesConflict:
    """R65 P1-05: complete 严格 CAS 路径冲突 raise AppError。"""

    @pytest.mark.asyncio
    async def test_complete_wrong_lease_version_raises_conflict(self, real_store):
        """complete 错误 lease_version → raise AppError(OUTBOX_LEASE_VERSION_CONFLICT)。"""
        rh = "rh_r65_complete_wrong" + "0" * 47
        eid = await real_store.add_outbox_event(
            action_id="act_r65_complete_wrong",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1(claim 后),用错误的 99 → raise
        with pytest.raises(AppError) as exc_info:
            await real_store.complete_outbox_event(
                eid, external_id="ext",
                lease_owner="worker_A", request_hash=rh,
                lease_version=99,  # 错误
            )
        # R65 P1-05: 验证错误码 + params
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        params = exc_info.value.params
        assert params["event_id"] == eid
        assert params["expected_lease_version"] == 99
        assert params["operation"] == "complete"

    @pytest.mark.asyncio
    async def test_complete_correct_lease_version_succeeds(self, real_store):
        """complete 正确 lease_version → CAS 成功(True)。"""
        rh = "rh_r65_complete_ok" + "0" * 49
        eid = await real_store.add_outbox_event(
            action_id="act_r65_complete_ok",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1,正确版本 → CAS 成功
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext_ok",
            lease_owner="worker_A", request_hash=rh,
            lease_version=1,
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT status, external_id FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == "ext_ok"


# ════════════════════════════════════════════════════════════════
# 4. fail_outbox_event 错误 lease_version → raise AppError
# ════════════════════════════════════════════════════════════════

class TestFailWrongVersionRaisesConflict:
    """R65 P1-05: fail 严格 CAS 路径冲突 raise AppError。"""

    @pytest.mark.asyncio
    async def test_fail_wrong_lease_version_raises_conflict(self, real_store):
        """fail 错误 lease_version → raise AppError(OUTBOX_LEASE_VERSION_CONFLICT)。"""
        rh = "rh_r65_fail_wrong" + "0" * 49
        eid = await real_store.add_outbox_event(
            action_id="act_r65_fail_wrong",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
            max_attempts=3,
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1(claim 后),用错误的 99 → raise
        with pytest.raises(AppError) as exc_info:
            await real_store.fail_outbox_event(
                eid, error_msg="boom",
                lease_owner="worker_A", request_hash=rh,
                lease_version=99,  # 错误
            )
        # R65 P1-05: 验证错误码 + params
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        params = exc_info.value.params
        assert params["event_id"] == eid
        assert params["expected_lease_version"] == 99
        assert params["operation"] == "fail"

    @pytest.mark.asyncio
    async def test_fail_correct_version_preserves_lease_version(self, real_store):
        """fail 正确版本 → retryable 路径不重置 lease_version(保留单调递增)。"""
        rh = "rh_r65_fail_ok" + "0" * 51
        eid = await real_store.add_outbox_event(
            action_id="act_r65_fail_ok",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
            max_attempts=3,
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1,fail → retryable
        result = await real_store.fail_outbox_event(
            eid, error_msg="boom",
            lease_owner="worker_A", request_hash=rh,
            lease_version=1,
        )
        assert result == "retryable"
        # R65 P1-05: lease_version 不重置为 0(保留单调递增,防 ABA)
        cursor = await real_store._db.execute(
            "SELECT lease_version, status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1  # 保留 lease_version(关键不变量)
        assert row[1] == "pending"


# ════════════════════════════════════════════════════════════════
# 5. renew_outbox_lease 递增 lease_version(1 → 2)+ 错误版本 raise
# ════════════════════════════════════════════════════════════════

class TestRenewIncrementsLeaseVersion:
    """R65 P1-05: renew_outbox_lease 递增 lease_version + 严格 CAS 冲突 raise。"""

    @pytest.mark.asyncio
    async def test_renew_increments_lease_version(self, real_store):
        """renew 成功 → lease_version += 1(1 → 2)。"""
        rh = "rh_r65_renew" + "0" * 53
        eid = await real_store.add_outbox_event(
            action_id="act_r65_renew",
            effect_type="r2_put",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_long", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1,renew → 2
        ok = await real_store.renew_outbox_lease(
            eid, lease_owner="worker_long", request_hash=rh,
            lease_version=1, lease_duration_seconds=300,
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT lease_version FROM outbox_events WHERE id=?", (eid,),
        )
        assert (await cursor.fetchone())[0] == 2

    @pytest.mark.asyncio
    async def test_renew_wrong_lease_version_raises_conflict(self, real_store):
        """renew 错误 lease_version → raise AppError(OUTBOX_LEASE_VERSION_CONFLICT)。"""
        rh = "rh_r65_renew_wrong" + "0" * 49
        eid = await real_store.add_outbox_event(
            action_id="act_r65_renew_wrong",
            effect_type="r2_put",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_long", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1(claim 后),用错误的 99 → raise
        with pytest.raises(AppError) as exc_info:
            await real_store.renew_outbox_lease(
                eid, lease_owner="worker_long", request_hash=rh,
                lease_version=99,  # 错误
                lease_duration_seconds=300,
            )
        # R65 P1-05: 验证错误码 + params
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        params = exc_info.value.params
        assert params["event_id"] == eid
        assert params["expected_lease_version"] == 99
        assert params["operation"] == "renew"


# ════════════════════════════════════════════════════════════════
# 6. lease_version 在 5 次 claim/fail/reclaim 循环中单调递增
# ════════════════════════════════════════════════════════════════

class TestLeaseVersionMonotonicOverCycles:
    """R65 P1-05: lease_version 在多次循环中单调递增(永不重置)。

    场景:同一事件被多次 claim → fail(retryable)→ reclaim → claim 循环,
    lease_version 必须单调递增(0 → 1 → 2 → 3 → 4 → 5),永不重置为 0。
    这保证 fencing token 永不重用 → 旧 worker 残留调用因版本不匹配被 CAS 拒绝
    (ABA 防御成立)。
    """

    @pytest.mark.asyncio
    async def test_lease_version_monotonic_over_5_cycles(self, real_store):
        """5 次 claim/fail/reclaim 循环:lease_version 单调递增 1,2,3,4,5。"""
        rh = "rh_r65_mono_5" + "0" * 53
        eid = await real_store.add_outbox_event(
            action_id="act_r65_mono_5",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
            max_attempts=100,  # 防止 retryable 转 DLQ
        )
        observed_versions: list[int] = []
        for cycle in range(5):
            # claim → lease_version += 1
            events = await real_store.claim_outbox_events(
                lease_owner=f"worker_cycle_{cycle}",
                lease_duration_seconds=60,
                limit=1,
            )
            assert len(events) == 1, f"cycle {cycle}: claim 失败"
            current_version = events[0]["lease_version"]
            observed_versions.append(current_version)
            # fail → retryable(lease_version 保留)
            result = await real_store.fail_outbox_event(
                eid, error_msg=f"cycle_{cycle}_fail",
                lease_owner=f"worker_cycle_{cycle}",
                request_hash=rh,
                lease_version=current_version,
            )
            assert result == "retryable", (
                f"cycle {cycle}: fail 应返回 retryable,实际 {result}"
            )
        # R65 P1-05: lease_version 必须单调递增 1,2,3,4,5
        assert observed_versions == [1, 2, 3, 4, 5], (
            f"lease_version 应单调递增 1-5,实际: {observed_versions}"
        )
        # 验证数据库中最终 lease_version=5(不重置为 0)
        cursor = await real_store._db.execute(
            "SELECT lease_version, status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 5  # 单调递增到 5
        assert row[1] == "pending"  # 最后一次 fail 后转回 pending

    @pytest.mark.asyncio
    async def test_lease_version_monotonic_over_reclaim_cycles(self, real_store):
        """5 次 claim/reclaim 循环(无 fail):lease_version 单调递增。"""
        rh = "rh_r65_mono_reclaim" + "0" * 47
        eid = await real_store.add_outbox_event(
            action_id="act_r65_mono_reclaim",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        observed_versions: list[int] = []
        for cycle in range(5):
            # claim → lease_version += 1
            events = await real_store.claim_outbox_events(
                lease_owner=f"worker_rc_{cycle}",
                lease_duration_seconds=60,
                limit=1,
            )
            assert len(events) == 1, f"cycle {cycle}: claim 失败"
            observed_versions.append(events[0]["lease_version"])
            # 模拟 lease 过期 + reclaim
            past_iso = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
            await real_store._db.execute(
                "UPDATE outbox_events SET lease_expires_at=? WHERE id=?",
                (past_iso, eid),
            )
            await real_store._db.commit()
            reclaimed = await real_store.reclaim_stale_outbox_leases(batch_size=10)
            assert reclaimed == 1, f"cycle {cycle}: reclaim 失败"
        # R65 P1-05: lease_version 必须单调递增 1,2,3,4,5
        assert observed_versions == [1, 2, 3, 4, 5], (
            f"lease_version 应单调递增 1-5(reclaim 路径),实际: {observed_versions}"
        )


# ════════════════════════════════════════════════════════════════
# 7. test_mode 参数已移除 → TypeError
# ════════════════════════════════════════════════════════════════

class TestTestModeParameterRemoved:
    """R65 P1-05: OutboxWorker 的 test_mode 参数彻底移除,传入即 TypeError。

    旧 R63/R64 实现保留 test_mode 参数(仅为向后兼容,R64 已不再控制 stub 行为);
    R65 P1-05 整改:从 __init__ 签名中彻底移除 test_mode,传入即 TypeError。
    这强制所有调用方(生产 + 测试)显式注入 provider,不再有 stub 分支残留。
    """

    def test_test_mode_true_raises_typeerror(self):
        """test_mode=True → TypeError(参数已移除)。"""
        from services.data_lifecycle import OutboxWorker
        with pytest.raises(TypeError):
            OutboxWorker(
                lease_owner="w", batch_size=1, test_mode=True,
            )

    def test_test_mode_false_raises_typeerror(self):
        """test_mode=False → TypeError(参数已移除,即使显式 False 也拒绝)。"""
        from services.data_lifecycle import OutboxWorker
        with pytest.raises(TypeError):
            OutboxWorker(
                lease_owner="w", batch_size=1, test_mode=False,
            )

    def test_no_test_mode_works(self):
        """不传 test_mode → 正常初始化(生产路径)。"""
        from services.data_lifecycle import OutboxWorker
        worker = OutboxWorker(lease_owner="w", batch_size=1)
        # 验证 test_mode 属性不存在(彻底移除)
        assert not hasattr(worker, "test_mode"), (
            "OutboxWorker.test_mode 属性应已彻底移除"
        )


# ════════════════════════════════════════════════════════════════
# 8. 补偿持久化:DLQ 时写入 audit_log(kind='compensation'),重启后可重放
# ════════════════════════════════════════════════════════════════

class TestCompensationPersistence:
    """R65 P1-05: 补偿持久化 — provider 失败进 DLQ 时持久化补偿意图。

    旧 R62 实现补偿仅作为 Python 回调(compensation_action 字段),worker 进程
    崩溃后补偿意图丢失(无法重启重放)。R65 P1-05 整改:provider 失败且进入
    DLQ 时,补偿意图持久化到 audit_log(kind='compensation'),保证 worker
    重启后 reconcile 流程仍可重放补偿(Python 回调仅作为实现细节)。
    """

    @pytest.mark.asyncio
    async def test_dlq_with_compensation_persists_audit_log(self, real_store):
        """provider 失败 + 进 DLQ → audit_log 写入 kind='compensation' 记录。"""
        from services.data_lifecycle import OutboxEnvelope, OutboxWorker

        async def _always_fail_provider(envelope: OutboxEnvelope):
            raise RuntimeError("provider permanent failure")

        rh = "rh_r65_comp_persist" + "0" * 48
        eid = await real_store.add_outbox_event(
            action_id="act_r65_comp_persist",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json=json.dumps({"text": "hi"}),
            max_attempts=1,  # 首次失败即超限 → DLQ
        )
        worker = OutboxWorker(
            lease_owner="worker_comp_test", batch_size=10,
            provider_registry={"telegram_send": _always_fail_provider},
        )
        result = await worker.run_once()
        assert result["claimed"] == 1
        assert result["dlq"] == 1
        # R65 P1-05: 验证 audit_log 写入了 kind='compensation' 记录
        cursor = await real_store._db.execute(
            "SELECT action, target_type, target_id, details "
            "FROM audit_log "
            "WHERE action='outbox_compensation_persisted' "
            "AND target_id=?",
            (str(eid),),
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1, (
            "DLQ 时应持久化补偿意图到 audit_log(kind='compensation')"
        )
        row = rows[0]
        assert row[0] == "outbox_compensation_persisted"
        assert row[1] == "outbox_event"
        assert row[2] == str(eid)
        # details 是 JSON,解析后验证 kind=compensation
        details = json.loads(row[3])
        assert details["kind"] == "compensation"
        assert details["event_id"] == eid
        assert details["action_id"] == "act_r65_comp_persist"
        assert details["effect_type"] == "telegram_send"
        assert "provider permanent failure" in details["error_msg"]

    @pytest.mark.asyncio
    async def test_compensation_persistence_survives_restart(self, real_store):
        """补偿意图持久化后,worker "重启"(重新查询 audit_log)仍可读取补偿记录。

        场景模拟:
        1. provider 失败 → DLQ + 持久化补偿意图到 audit_log
        2. worker "崩溃"(模拟重启:不再持有内存状态)
        3. reconcile 流程查询 audit_log(kind='compensation')仍能读到补偿记录
        """
        from services.data_lifecycle import OutboxEnvelope, OutboxWorker

        async def _fail_once_provider(envelope: OutboxEnvelope):
            raise RuntimeError("simulated provider crash")

        rh = "rh_r65_comp_restart" + "0" * 47
        eid = await real_store.add_outbox_event(
            action_id="act_r65_comp_restart",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
            max_attempts=1,  # 首次失败即超限 → DLQ
        )
        # 第一次 worker 运行 → DLQ + 持久化补偿意图
        worker1 = OutboxWorker(
            lease_owner="worker_will_crash", batch_size=10,
            provider_registry={"telegram_send": _fail_once_provider},
        )
        result = await worker1.run_once()
        assert result["dlq"] == 1
        # 模拟 worker 崩溃:丢弃 worker1 实例(内存状态丢失)
        del worker1
        # "重启"后 reconcile 流程查询 audit_log(kind='compensation')
        # 应仍能读到补偿记录(持久化不依赖进程内存)
        cursor = await real_store._db.execute(
            "SELECT details FROM audit_log "
            "WHERE action='outbox_compensation_persisted' "
            "AND target_id=?",
            (str(eid),),
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1, (
            "worker 重启后应能从 audit_log 读取补偿意图(持久化)"
        )
        details = json.loads(rows[0][0])
        assert details["kind"] == "compensation"
        assert details["event_id"] == eid
        assert details["action_id"] == "act_r65_comp_restart"


# ════════════════════════════════════════════════════════════════
# 9. complete/fail/renew 严格 CAS 冲突 raise 的错误码与 params 一致
# ════════════════════════════════════════════════════════════════

class TestCASConflictErrorConsistency:
    """R65 P1-05: complete/fail/renew CAS 冲突 raise 的错误码与 params 一致。

    所有三个方法的严格 CAS 路径冲突都 raise:
    - code = ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
    - params = {event_id, expected_lease_version, operation}
    - operation 分别为 'complete' / 'fail' / 'renew'
    """

    @pytest.mark.asyncio
    async def test_complete_conflict_params_contain_operation(self, real_store):
        """complete CAS 冲突 → params.operation == 'complete'。"""
        rh = "rh_r65_complete_op" + "0" * 50
        eid = await real_store.add_outbox_event(
            action_id="act_r65_complete_op",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        with pytest.raises(AppError) as exc_info:
            await real_store.complete_outbox_event(
                eid, external_id="ext",
                lease_owner="worker_A", request_hash=rh,
                lease_version=99,
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        assert exc_info.value.params["operation"] == "complete"

    @pytest.mark.asyncio
    async def test_fail_conflict_params_contain_operation(self, real_store):
        """fail CAS 冲突 → params.operation == 'fail'。"""
        rh = "rh_r65_fail_op" + "0" * 54
        eid = await real_store.add_outbox_event(
            action_id="act_r65_fail_op",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
            max_attempts=3,
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        with pytest.raises(AppError) as exc_info:
            await real_store.fail_outbox_event(
                eid, error_msg="boom",
                lease_owner="worker_A", request_hash=rh,
                lease_version=99,
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        assert exc_info.value.params["operation"] == "fail"

    @pytest.mark.asyncio
    async def test_renew_conflict_params_contain_operation(self, real_store):
        """renew CAS 冲突 → params.operation == 'renew'。"""
        rh = "rh_r65_renew_op" + "0" * 52
        eid = await real_store.add_outbox_event(
            action_id="act_r65_renew_op",
            effect_type="r2_put",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        with pytest.raises(AppError) as exc_info:
            await real_store.renew_outbox_lease(
                eid, lease_owner="worker_A", request_hash=rh,
                lease_version=99, lease_duration_seconds=300,
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        assert exc_info.value.params["operation"] == "renew"

    def test_error_code_registered_in_registry(self):
        """OUTBOX_LEASE_VERSION_CONFLICT 必须在 ErrorRegistry 中注册。"""
        from services.error_codes import ErrorRegistry
        assert ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT in ErrorRegistry.all_codes()
        # 验证元信息
        sev = ErrorRegistry.get_severity(
            ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        )
        assert sev == "critical"
        http_status = ErrorRegistry.get_http_status(
            ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        )
        assert http_status == 409  # Conflict
        # 验证 safe_params 包含 event_id / expected_lease_version / operation
        safe_params = ErrorRegistry.get_safe_params(
            ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        )
        assert "event_id" in safe_params
        assert "expected_lease_version" in safe_params
        assert "operation" in safe_params
