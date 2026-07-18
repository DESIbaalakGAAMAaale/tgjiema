"""R63 P0-05 + P1-02: OutboxWorker stub 静默成功 + 冲突处理异常字符串猜测 整改测试。

被测目标:
- ``services.data_lifecycle.OutboxWorker`` —— R63 P0-05 整改:
    * ``__init__`` 新增 ``test_mode: bool = False`` 参数
    * ``run_once``: ``provider_registry is None`` 且非 ``test_mode`` → raise AppError
    * ``run_once``: provider 调用签名扩展为
      ``async (payload_json, request_hash, idempotency_key) -> external_id``
    * ``run_once``: complete 时使用 CAS ``WHERE status='in_flight' AND lease_owner=?
      AND request_hash=?``,并保存 ``external_id``
    * 新增 ``validate_providers()``: 校验每个枚举 effect type 恰有一个 provider
    * 新增 ``reclaim_stale_leases()``: 回收过期 lease(in_flight + 过期 → pending)
- ``database.cache_store.CacheStore.complete_outbox_event`` —— R63 P0-05 整改:
    * 新增 ``external_id`` / ``lease_owner`` / ``request_hash`` 参数
    * SQL 改为 ``UPDATE ... SET status='completed', external_id=? WHERE id=?
      AND status='in_flight' AND lease_owner=? AND request_hash=?``
    * 新增 ``reclaim_stale_outbox_leases()`` / ``renew_outbox_lease()`` 辅助方法
- ``services.data_lifecycle.execute_high_risk_command_uow`` —— R63 P1-02 整改:
    * outbox 冲突处理改用 ``sqlite3.IntegrityError`` + UNIQUE constraint name 判断
      (非泛 ``unique``/``constraint`` 子串匹配)
    * 冲突后 SELECT 既有行,逐字段验证 action_id/effect_type/target/request_hash
      + payload sha256 digest 一致,否则抛 ``IDEMPOTENCY_CONFLICT``

测试覆盖:
P0-05 (OutboxWorker stub 静默成功):
  1. run_once 无 provider 且非 test_mode → raise AppError(fail-fast)
  2. run_once 无 provider + test_mode=True → stub 模式正常 complete
  3. run_once 有 provider_registry → 正常调用 provider
  4. validate_providers() registry=None → 返回全部 missing
  5. validate_providers() 部分缺失 → 返回 missing 列表
  6. validate_providers() 全覆盖 → 返回空 list
  7. validate_providers() 自定义 required_effect_types
  8. reclaim_stale_leases() 回收过期 lease
  9. reclaim_stale_leases() 无过期 → 返回 0
  10. reclaim_stale_leases() 保留 attempt_count
  11. run_once 调用 provider 传入 (payload_json, request_hash, idempotency_key)
  12. run_once 保存 external_id 到 outbox_events
  13. complete_outbox_event CAS lease_owner + request_hash 双重校验
  14. complete_outbox_event 错误 lease_owner → False
  15. complete_outbox_event 错误 request_hash → False
  16. complete_outbox_event 兼容路径(无 CAS 参数)
  17. renew_outbox_lease 续约 lease
P1-02 (outbox 冲突处理异常字符串猜测):
  18. UNIQUE 冲突 + 同 payload → 幂等(不抛错)
  19. UNIQUE 冲突 + 不同 payload → IDEMPOTENCY_CONFLICT
  20. 非 UNIQUE IntegrityError(CHECK 约束)→ 抛错(不静默视为已排队)
  21. UNIQUE 冲突后逐字段验证(action/effect/target/hash 一致)
"""
from __future__ import annotations

import inspect
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from services.error_codes import AppError

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
# Fixture: 真实 SQLite 临时数据库(由 init() 创建含 R62 + R63 约束的表)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建 outbox_events 表(含 R63 P0-05 新增 external_id 列
    + idx_outbox_events_in_flight_lease 索引)+ effect_receipts 表。
    """
    tmpdir = tempfile.mkdtemp(prefix="r63_p0_5_p1_2_test_")
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
# 辅助函数: 为 UoW 集成测试搭建 break-glass 双人审批 + command_executions
# ════════════════════════════════════════════════════════════════

async def _setup_break_glass_approval(
    store, action_id: str, canonical_hash: str, principal_id: int = 1,
):
    """搭建 break-glass 双人审批环境(command_executions + command_approvals)。

    用于 ``execute_high_risk_command_uow`` 集成测试:
    - 创建 command_approvals 表(若不存在,migration 由 UoW 调用方负责)
    - 插入 approved command_executions 行
    - 签发 2 个 approver(principal_id=2/3)的 MFA receipt
    - 插入 2 条 break_glass 审批记录(decision=approved, request_hash, 未过期)
    - bootstrap principal_id 为 super_admin(权限重鉴权通过)

    Returns:
        (now_iso, future_iso) 元组
    """
    import datetime as _dt
    import os
    from services import command_bus
    from admin.mfa import issue_mfa_receipt

    now = command_bus._now_iso()
    future_iso = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
    ).isoformat()

    # ── 1. command_executions(approved 状态)──
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        "request_hash, result, created_at, updated_at, requires_approval, approved_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?)",
        (action_id, "data_lifecycle_break_glass", principal_id,
         command_bus.CMD_STATUS_APPROVED,
         canonical_hash, now, now, 1, now),
    )

    # ── 2. command_approvals 表(若不存在则创建,与 test_r51_p1 一致)──
    await store._db.execute(
        "CREATE TABLE IF NOT EXISTS command_approvals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "action_id TEXT NOT NULL, "
        "approver_id BIGINT NOT NULL, "
        "approval_type TEXT NOT NULL, "
        "mfa_receipt TEXT, "
        "approved_at TEXT NOT NULL, "
        "metadata_json TEXT, "
        "UNIQUE(action_id, approver_id))"
    )
    # R58 P0-2: 补列(幂等,重复添加忽略)
    for col, col_def in [
        ("decision", "TEXT NOT NULL DEFAULT 'approved'"),
        ("request_hash", "TEXT NOT NULL DEFAULT ''"),
        ("permission", "TEXT NOT NULL DEFAULT ''"),
        ("expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("consumed_at", "TEXT"),
        ("revoked_at", "TEXT"),
    ]:
        try:
            await store._db.execute(
                f"ALTER TABLE command_approvals ADD COLUMN {col} {col_def}"
            )
        except Exception:
            pass  # 列已存在(幂等)

    # ── 3. 签发 2 个 approver 的 MFA receipt(R59 P0-03: 必须真实签发)──
    os.environ["MFA_RECEIPT_SIGNING_KEY"] = (
        "test_signing_key_for_r63_uow_integration_32b"
    )
    try:
        r2 = issue_mfa_receipt(
            principal_id=2, purpose="break_glass_approval",
            action_hash=canonical_hash, amr=["totp"], ttl_seconds=300,
        )
        r3 = issue_mfa_receipt(
            principal_id=3, purpose="break_glass_approval",
            action_hash=canonical_hash, amr=["totp"], ttl_seconds=300,
        )
    finally:
        del os.environ["MFA_RECEIPT_SIGNING_KEY"]

    # ── 4. 插入 2 条 break_glass 审批记录 ──
    for approver_id, receipt in [(2, r2), (3, r3)]:
        await store._db.execute(
            "INSERT INTO command_approvals "
            "(action_id, approver_id, approval_type, mfa_receipt, approved_at, "
            "metadata_json, decision, request_hash, permission, expires_at, "
            "consumed_at, revoked_at) "
            "VALUES (?, ?, 'break_glass', ?, ?, NULL, 'approved', ?, "
            "'break_glass', ?, NULL, NULL)",
            (action_id, approver_id, receipt, now, canonical_hash, future_iso),
        )
    await store._db.commit()

    # ── 5. bootstrap super_admin(权限重鉴权通过)──
    await store.bootstrap_admin_principal(
        principal_id=principal_id, username="admin", roles=["super_admin"],
    )
    return now, future_iso


def _make_grant(action_id: str, canonical_hash: str, future_iso: str, now_iso: str):
    """构造一个 ApprovalGrant(用于 UoW 集成测试,跳过 _verify_break_glass_two_person_approval)。"""
    import datetime as _dt
    from services.data_lifecycle import ApprovalGrant
    return ApprovalGrant(
        action_id=action_id,
        approver_ids=[2, 3],
        jti_list=["jti_2", "jti_3"],
        expected_principal_id=1,
        permission="break_glass",
        expires_at=future_iso,
        consumed_at_now=now_iso,
        now_unix=int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
        request_hash=canonical_hash,
    )


# ════════════════════════════════════════════════════════════════
# P0-05: OutboxWorker test_mode guard (防 stub 误启动)
# ════════════════════════════════════════════════════════════════

class TestOutboxWorkerTestModeGuard:
    """R63 P0-05: provider_registry=None + 非 test_mode → run_once raise AppError。"""

    @pytest.mark.asyncio
    async def test_run_once_raises_runtime_error_when_no_provider_and_not_test_mode(
        self, real_store,
    ):
        """生产模式(test_mode=False,默认)下 provider_registry=None → AppError。

        场景:管理员误启动默认配置的 worker,旧实现会静默 complete 所有外部副作用,
        造成业务状态与外部世界失配。新实现 fail-fast raise AppError。
        """
        from services.data_lifecycle import OutboxWorker
        # 准备 pending 事件(确保 fail-fast 在 claim 之前发生)
        await real_store.add_outbox_event(
            action_id="act_p0_5_guard",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_p0_5_guard" + "0" * 53,
            payload_json="{}",
        )
        # 默认 test_mode=False,无 provider_registry
        worker = OutboxWorker(lease_owner="worker_prod", batch_size=10)
        assert worker.test_mode is False
        assert worker.provider_registry is None
        # run_once 必须 raise AppError(fail-fast)
        with pytest.raises(AppError) as exc_info:
            await worker.run_once()
        err_msg = str(exc_info.value)
        # 错误消息含关键提示(便于运维定位)
        # 新 AppError 消息(zh-CN): "Outbox provider 注册表未配置,生产模式拒绝 stub 启动(reason=..., trace_id=...)"
        assert "outbox provider" in err_msg.lower()
        assert "stub" in err_msg.lower()
        # 验证 fail-fast 发生在 claim 之前(事件仍为 pending,未被 claim)
        cursor = await real_store._db.execute(
            "SELECT status FROM outbox_events WHERE action_id=?",
            ("act_p0_5_guard",),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"  # 未被 claim

    @pytest.mark.asyncio
    async def test_run_once_works_with_test_mode_true_and_no_provider(
        self, real_store,
    ):
        """test_mode=True + provider_registry=None → stub 模式正常 complete。"""
        from services.data_lifecycle import OutboxWorker
        await real_store.add_outbox_event(
            action_id="act_p0_5_stub",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_p0_5_stub" + "0" * 53,
            payload_json="{}",
        )
        worker = OutboxWorker(
            lease_owner="worker_stub", batch_size=10, test_mode=True,
        )
        assert worker.test_mode is True
        result = await worker.run_once()
        assert result["claimed"] == 1
        assert result["completed"] == 1
        assert result["failed"] == 0
        assert result["dlq"] == 0

    @pytest.mark.asyncio
    async def test_run_once_works_with_provider_registry_no_test_mode(
        self, real_store,
    ):
        """有 provider_registry + test_mode=False → 正常调用 provider(生产路径)。"""
        from services.data_lifecycle import OutboxWorker

        async def _provider(payload_json, request_hash, idempotency_key):
            return "ext_id_123"

        await real_store.add_outbox_event(
            action_id="act_p0_5_prod",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_p0_5_prod" + "0" * 53,
            payload_json="{}",
        )
        worker = OutboxWorker(
            lease_owner="worker_prod",
            batch_size=10,
            provider_registry={"telegram_message": _provider},
        )
        # test_mode 默认 False,但有 provider_registry → 不 raise
        assert worker.test_mode is False
        result = await worker.run_once()
        assert result["claimed"] == 1
        assert result["completed"] == 1
        assert result["failed"] == 0
        assert result["dlq"] == 0

    @pytest.mark.asyncio
    async def test_test_mode_default_is_false(self):
        """OutboxWorker 默认 test_mode=False(生产安全默认)。"""
        from services.data_lifecycle import OutboxWorker
        worker = OutboxWorker(lease_owner="w", batch_size=1)
        assert worker.test_mode is False

    @pytest.mark.asyncio
    async def test_test_mode_explicit_false_raises(self, real_store):
        """显式 test_mode=False + 无 provider → raise AppError(防显式误配置)。"""
        from services.data_lifecycle import OutboxWorker
        await real_store.add_outbox_event(
            action_id="act_p0_5_explicit_false",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_p0_5_explicit" + "0" * 51,
            payload_json="{}",
        )
        worker = OutboxWorker(
            lease_owner="w", batch_size=10, test_mode=False,
        )
        with pytest.raises(AppError):
            await worker.run_once()


# ════════════════════════════════════════════════════════════════
# P0-05: OutboxWorker.validate_providers() readiness 检查
# ════════════════════════════════════════════════════════════════

class TestOutboxWorkerValidateProviders:
    """R63 P0-05: validate_providers() 校验每个 effect type 恰有一个 provider。"""

    def test_validate_providers_returns_all_missing_when_registry_none(self):
        """provider_registry=None → 返回全部 CRITICAL_EFFECT_TYPES(missing)。"""
        from services.data_lifecycle import OutboxWorker
        worker = OutboxWorker(
            lease_owner="w", batch_size=1, test_mode=True,
        )
        missing = worker.validate_providers()
        # CRITICAL_EFFECT_TYPES 有 9 个枚举值
        from services.effect_receipts import CRITICAL_EFFECT_TYPES
        assert len(missing) == len(CRITICAL_EFFECT_TYPES)
        for et in CRITICAL_EFFECT_TYPES:
            assert et in missing

    def test_validate_providers_returns_missing_list_for_partial_registry(self):
        """部分 effect_type 缺 provider → 返回 missing 列表。"""
        from services.data_lifecycle import OutboxWorker
        from services.effect_receipts import CRITICAL_EFFECT_TYPES
        # 只注册 1 个 provider
        worker = OutboxWorker(
            lease_owner="w", batch_size=1,
            provider_registry={"telegram_send": AsyncMock()},
        )
        missing = worker.validate_providers()
        # 应返回除 telegram_send 外的所有 effect types
        expected_missing = set(CRITICAL_EFFECT_TYPES) - {"telegram_send"}
        assert set(missing) == expected_missing
        assert len(missing) == len(CRITICAL_EFFECT_TYPES) - 1

    def test_validate_providers_returns_empty_when_all_covered(self):
        """所有 CRITICAL_EFFECT_TYPES 都有 provider → 返回空 list(readiness OK)。"""
        from services.data_lifecycle import OutboxWorker
        from services.effect_receipts import CRITICAL_EFFECT_TYPES
        registry = {et: AsyncMock() for et in CRITICAL_EFFECT_TYPES}
        worker = OutboxWorker(
            lease_owner="w", batch_size=1,
            provider_registry=registry,
        )
        missing = worker.validate_providers()
        assert missing == []

    def test_validate_providers_with_custom_required_types(self):
        """自定义 required_effect_types → 仅校验指定的 effect types。"""
        from services.data_lifecycle import OutboxWorker
        worker = OutboxWorker(
            lease_owner="w", batch_size=1,
            provider_registry={"telegram_send": AsyncMock()},
        )
        # 自定义只要求 telegram_send + r2_put
        missing = worker.validate_providers(
            required_effect_types={"telegram_send", "r2_put"},
        )
        assert missing == ["r2_put"]  # r2_put 缺失

    def test_validate_providers_treats_non_callable_as_missing(self):
        """provider 值为 None / 非 callable → 视为缺失。"""
        from services.data_lifecycle import OutboxWorker
        worker = OutboxWorker(
            lease_owner="w", batch_size=1,
            provider_registry={
                "telegram_send": AsyncMock(),
                "r2_put": None,  # None → missing
                "purge": "not_a_callable",  # 非 callable → missing
            },
        )
        missing = worker.validate_providers(
            required_effect_types={"telegram_send", "r2_put", "purge"},
        )
        assert set(missing) == {"r2_put", "purge"}


# ════════════════════════════════════════════════════════════════
# P0-05: OutboxWorker.reclaim_stale_leases() 回收过期 lease
# ════════════════════════════════════════════════════════════════

class TestOutboxWorkerReclaimStaleLeases:
    """R63 P0-05: reclaim_stale_leases() 回收过期 lease(in_flight + 过期 → pending)。"""

    @pytest.mark.asyncio
    async def test_reclaim_stale_leases_reclaims_expired(self, real_store):
        """过期 lease 的 in_flight 行 → 回收为 pending。"""
        from services.data_lifecycle import OutboxWorker
        eid = await real_store.add_outbox_event(
            action_id="act_reclaim_1",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_reclaim_1" + "0" * 53,
            payload_json="{}",
        )
        # claim → in_flight
        await real_store.claim_outbox_events(
            lease_owner="worker_dead", lease_duration_seconds=60, limit=1,
        )
        # 模拟 worker 崩溃:手动把 lease_expires_at 设为过去时间
        past_iso = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        await real_store._db.execute(
            "UPDATE outbox_events SET lease_expires_at=? WHERE id=?",
            (past_iso, eid),
        )
        await real_store._db.commit()
        # reclaim
        worker = OutboxWorker(
            lease_owner="worker_reclaimer", batch_size=10, test_mode=True,
        )
        reclaimed = await worker.reclaim_stale_leases()
        assert reclaimed == 1
        # 验证状态:in_flight → pending,lease_owner/lease_expires_at 清空
        cursor = await real_store._db.execute(
            "SELECT status, lease_owner, lease_expires_at FROM outbox_events "
            "WHERE id=?",
            (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"
        assert row[1] is None
        assert row[2] is None

    @pytest.mark.asyncio
    async def test_reclaim_stale_leases_no_stale_returns_zero(self, real_store):
        """无过期 lease → 返回 0。"""
        from services.data_lifecycle import OutboxWorker
        # 添加 1 条 pending(未 claim) + 1 条 in_flight(未过期)
        await real_store.add_outbox_event(
            action_id="act_reclaim_2",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_reclaim_2" + "0" * 53,
            payload_json="{}",
        )
        await real_store.add_outbox_event(
            action_id="act_reclaim_3",
            effect_type="telegram_message",
            target="chat:2",
            request_hash="rh_reclaim_3" + "0" * 53,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_active", lease_duration_seconds=60, limit=1,
        )
        worker = OutboxWorker(
            lease_owner="w", batch_size=10, test_mode=True,
        )
        reclaimed = await worker.reclaim_stale_leases()
        assert reclaimed == 0

    @pytest.mark.asyncio
    async def test_reclaim_stale_leases_keeps_attempt_count(self, real_store):
        """回收后 attempt_count 不变(失败的尝试仍计入重试上限)。"""
        from services.data_lifecycle import OutboxWorker
        eid = await real_store.add_outbox_event(
            action_id="act_reclaim_4",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_reclaim_4" + "0" * 53,
            payload_json="{}",
            max_attempts=3,
        )
        # claim 2 次(attempt_count=2)
        await real_store.claim_outbox_events(
            lease_owner="worker_a", lease_duration_seconds=60, limit=1,
        )
        # fail 转回 pending(attempt_count 保持 1,但 fail_outbox_event 不动 attempt_count)
        # 再 claim(attempt_count → 2)
        # 先 fail 第一次
        await real_store.fail_outbox_event(eid, error_msg="try1")
        await real_store.claim_outbox_events(
            lease_owner="worker_b", lease_duration_seconds=60, limit=1,
        )
        # 验证 attempt_count=2
        cursor = await real_store._db.execute(
            "SELECT attempt_count FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 2
        # 模拟 lease 过期
        past_iso = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        await real_store._db.execute(
            "UPDATE outbox_events SET lease_expires_at=? WHERE id=?",
            (past_iso, eid),
        )
        await real_store._db.commit()
        # reclaim
        worker = OutboxWorker(lease_owner="w", batch_size=10, test_mode=True)
        reclaimed = await worker.reclaim_stale_leases()
        assert reclaimed == 1
        # attempt_count 仍为 2(不递减)
        cursor = await real_store._db.execute(
            "SELECT attempt_count, status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 2
        assert row[1] == "pending"

    @pytest.mark.asyncio
    async def test_reclaim_stale_leases_batch_size_limit(self, real_store):
        """batch_size 限制单次回收行数。"""
        from services.data_lifecycle import OutboxWorker
        # 添加 5 条事件并全部 claim + 设为过期
        for i in range(5):
            await real_store.add_outbox_event(
                action_id=f"act_reclaim_batch_{i}",
                effect_type="telegram_message",
                target=f"chat:{i}",
                request_hash=f"rh_reclaim_batch_{i}" + "0" * 50,
                payload_json="{}",
            )
        await real_store.claim_outbox_events(
            lease_owner="worker_dead", lease_duration_seconds=60, limit=10,
        )
        past_iso = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        await real_store._db.execute(
            "UPDATE outbox_events SET lease_expires_at=?",
            (past_iso,),
        )
        await real_store._db.commit()
        # batch_size=2 → 只回收 2 条
        worker = OutboxWorker(lease_owner="w", batch_size=10, test_mode=True)
        reclaimed = await worker.reclaim_stale_leases(batch_size=2)
        assert reclaimed == 2


# ════════════════════════════════════════════════════════════════
# P0-05: OutboxWorker.run_once provider 签名 + external_id 保存
# ════════════════════════════════════════════════════════════════

class TestOutboxWorkerProviderSignature:
    """R63 P0-05: provider 签名 (payload_json, request_hash, idempotency_key) -> external_id。"""

    @pytest.mark.asyncio
    async def test_run_once_calls_provider_with_three_args(self, real_store):
        """provider 被调用时收到 3 个参数:payload_json, request_hash, idempotency_key。"""
        from services.data_lifecycle import OutboxWorker
        captured_calls = []

        async def _capturing_provider(payload_json, request_hash, idempotency_key):
            captured_calls.append({
                "payload_json": payload_json,
                "request_hash": request_hash,
                "idempotency_key": idempotency_key,
            })
            return "ext_id"

        rh = "rh_signature" + "0" * 51
        await real_store.add_outbox_event(
            action_id="act_signature_1",
            effect_type="telegram_message",
            target="chat:1",
            request_hash=rh,
            payload_json=json.dumps({"text": "hi"}),
        )
        worker = OutboxWorker(
            lease_owner="w", batch_size=10,
            provider_registry={"telegram_message": _capturing_provider},
        )
        await worker.run_once()
        assert len(captured_calls) == 1
        call = captured_calls[0]
        # payload_json 透传
        assert json.loads(call["payload_json"])["text"] == "hi"
        # request_hash 从 outbox event 读取
        assert call["request_hash"] == rh
        # idempotency_key = action_id:request_hash
        assert call["idempotency_key"] == f"act_signature_1:{rh}"

    @pytest.mark.asyncio
    async def test_run_once_saves_external_id_to_outbox_event(self, real_store):
        """provider 返回的 external_id 被保存到 outbox_events.external_id 列。"""
        from services.data_lifecycle import OutboxWorker

        async def _provider(payload_json, request_hash, idempotency_key):
            return "tg_msg_id_98765"

        eid = await real_store.add_outbox_event(
            action_id="act_ext_id",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_ext_id" + "0" * 55,
            payload_json="{}",
        )
        worker = OutboxWorker(
            lease_owner="w", batch_size=10,
            provider_registry={"telegram_message": _provider},
        )
        await worker.run_once()
        # 验证 external_id 已保存
        cursor = await real_store._db.execute(
            "SELECT status, external_id FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == "tg_msg_id_98765"

    @pytest.mark.asyncio
    async def test_run_once_external_id_none_saved_as_empty(self, real_store):
        """provider 返回 None → external_id 保存为空字符串(不报错)。"""
        from services.data_lifecycle import OutboxWorker

        async def _provider(payload_json, request_hash, idempotency_key):
            return None  # 部分 provider 可能无 external_id

        eid = await real_store.add_outbox_event(
            action_id="act_ext_none",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_ext_none" + "0" * 53,
            payload_json="{}",
        )
        worker = OutboxWorker(
            lease_owner="w", batch_size=10,
            provider_registry={"telegram_message": _provider},
        )
        result = await worker.run_once()
        assert result["completed"] == 1
        cursor = await real_store._db.execute(
            "SELECT external_id FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "" or row[0] is None

    @pytest.mark.asyncio
    async def test_run_once_complete_uses_cas_with_lease_owner(self, real_store):
        """complete 时 CAS 校验 lease_owner(其它 worker 不能 complete 本 worker 的 lease)。"""
        from services.data_lifecycle import OutboxWorker

        async def _provider(payload_json, request_hash, idempotency_key):
            return "ext"

        eid = await real_store.add_outbox_event(
            action_id="act_cas_lease",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_cas_lease" + "0" * 54,
            payload_json="{}",
        )
        # 用 worker_A claim
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # 用 worker_B 尝试 complete(传入错误的 lease_owner)→ CAS 应失败
        # 直接调用 complete_outbox_event 验证 CAS
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_B",  # 错误的 lease_owner
            request_hash="rh_cas_lease" + "0" * 54,
        )
        assert ok is False
        # 验证状态仍为 in_flight(未被 complete)
        cursor = await real_store._db.execute(
            "SELECT status, lease_owner FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "in_flight"
        assert row[1] == "worker_A"

    @pytest.mark.asyncio
    async def test_run_once_complete_uses_cas_with_request_hash(self, real_store):
        """complete 时 CAS 校验 request_hash(错配的 hash 不能 complete)。"""
        from services.data_lifecycle import OutboxWorker

        eid = await real_store.add_outbox_event(
            action_id="act_cas_rh",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_cas_rh_correct" + "0" * 47,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_X", lease_duration_seconds=60, limit=1,
        )
        # 传入错误的 request_hash
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_X",
            request_hash="rh_cas_rh_WRONG" + "0" * 48,  # 错误的 hash
        )
        assert ok is False
        # 正确的 request_hash 才能 complete
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_X",
            request_hash="rh_cas_rh_correct" + "0" * 47,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_run_once_stub_mode_also_uses_cas(self, real_store):
        """stub 模式(test_mode=True)complete 也走 CAS 路径(防 stub 误完成非己事件)。"""
        from services.data_lifecycle import OutboxWorker
        eid = await real_store.add_outbox_event(
            action_id="act_stub_cas",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_stub_cas" + "0" * 53,
            payload_json="{}",
        )
        # 用 worker_X claim
        await real_store.claim_outbox_events(
            lease_owner="worker_X", lease_duration_seconds=60, limit=1,
        )
        # 用 worker_Y 的 stub 尝试 complete(worker_Y 不是 lease 持有者)
        # stub 模式下 run_once 会先 claim 自己的事件(但已被 worker_X claim,无 pending)
        worker_Y = OutboxWorker(
            lease_owner="worker_Y", batch_size=10, test_mode=True,
        )
        result = await worker_Y.run_once()
        # worker_Y claim 不到任何事件(已被 worker_X claim)
        assert result["claimed"] == 0
        # 验证事件仍为 in_flight(worker_X 持有)
        cursor = await real_store._db.execute(
            "SELECT status, lease_owner FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "in_flight"
        assert row[1] == "worker_X"


# ════════════════════════════════════════════════════════════════
# P0-05: CacheStore.complete_outbox_event CAS 行为
# ════════════════════════════════════════════════════════════════

class TestCompleteOutboxEventCAS:
    """R63 P0-05: complete_outbox_event CAS 条件(lease_owner + request_hash)。"""

    @pytest.mark.asyncio
    async def test_complete_with_correct_cas_succeeds(self, real_store):
        """正确的 lease_owner + request_hash → CAS 成功。"""
        rh = "rh_cas_ok" + "0" * 56
        eid = await real_store.add_outbox_event(
            action_id="act_cas_ok",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_ok", lease_duration_seconds=60, limit=1,
        )
        ok = await real_store.complete_outbox_event(
            eid, external_id="r2_key_123",
            lease_owner="worker_ok", request_hash=rh,
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT status, external_id, lease_owner FROM outbox_events WHERE id=?",
            (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == "r2_key_123"

    @pytest.mark.asyncio
    async def test_complete_wrong_lease_owner_returns_false(self, real_store):
        """错误的 lease_owner → CAS 失败,返回 False。"""
        rh = "rh_cas_wo" + "0" * 55
        eid = await real_store.add_outbox_event(
            action_id="act_cas_wo",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_right", lease_duration_seconds=60, limit=1,
        )
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_wrong",  # 错误
            request_hash=rh,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_complete_wrong_request_hash_returns_false(self, real_store):
        """错误的 request_hash → CAS 失败,返回 False。"""
        rh = "rh_cas_wh" + "0" * 55
        eid = await real_store.add_outbox_event(
            action_id="act_cas_wh",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_ok", lease_duration_seconds=60, limit=1,
        )
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_ok",
            request_hash="rh_WRONG" + "0" * 56,  # 错误
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_complete_no_cas_args_backward_compat(self, real_store):
        """无 lease_owner/request_hash → 走兼容路径(仅 status CAS)。"""
        eid = await real_store.add_outbox_event(
            action_id="act_cas_compat",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash="rh_compat" + "0" * 55,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_compat", lease_duration_seconds=60, limit=1,
        )
        # 不传 lease_owner / request_hash → 兼容路径
        ok = await real_store.complete_outbox_event(eid, external_id="ext")
        assert ok is True

    @pytest.mark.asyncio
    async def test_complete_saves_external_id(self, real_store):
        """complete 时 external_id 被保存(供事后对账与人工重放)。"""
        rh = "rh_ext_save" + "0" * 54
        eid = await real_store.add_outbox_event(
            action_id="act_ext_save",
            effect_type="telegram_message",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_save", lease_duration_seconds=60, limit=1,
        )
        await real_store.complete_outbox_event(
            eid, external_id="msg_id_42",
            lease_owner="worker_save", request_hash=rh,
        )
        cursor = await real_store._db.execute(
            "SELECT external_id FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "msg_id_42"


# ════════════════════════════════════════════════════════════════
# P0-05: CacheStore.renew_outbox_lease 续约 lease
# ════════════════════════════════════════════════════════════════

class TestRenewOutboxLease:
    """R63 P0-05: renew_outbox_lease 续约 lease(长 provider 调用防超时回收)。"""

    @pytest.mark.asyncio
    async def test_renew_lease_extends_expires_at(self, real_store):
        """renew_outbox_lease 续约 lease_expires_at。"""
        rh = "rh_renew" + "0" * 56
        eid = await real_store.add_outbox_event(
            action_id="act_renew",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_long", lease_duration_seconds=10, limit=1,
        )
        # 续约 300 秒
        ok = await real_store.renew_outbox_lease(
            eid, lease_owner="worker_long", request_hash=rh,
            lease_duration_seconds=300,
        )
        assert ok is True
        # 验证 lease_expires_at 已延后(>now+200s)
        cursor = await real_store._db.execute(
            "SELECT lease_expires_at FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        new_expires = datetime.fromisoformat(row[0])
        # 应该在 now+200s 之后(300s 续约)
        assert new_expires > datetime.utcnow() + timedelta(seconds=200)

    @pytest.mark.asyncio
    async def test_renew_lease_wrong_owner_returns_false(self, real_store):
        """非 lease 持有者不能续约 → False。"""
        rh = "rh_renew_wo" + "0" * 54
        eid = await real_store.add_outbox_event(
            action_id="act_renew_wo",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_owner", lease_duration_seconds=60, limit=1,
        )
        ok = await real_store.renew_outbox_lease(
            eid, lease_owner="worker_other", request_hash=rh,
            lease_duration_seconds=300,
        )
        assert ok is False


# ════════════════════════════════════════════════════════════════
# P1-02: outbox 冲突处理 — sqlite3.IntegrityError + 字段验证
# ════════════════════════════════════════════════════════════════

class TestOutboxConflictHandling:
    """R63 P1-02: outbox 冲突处理改用错误码 + 字段验证(非字符串猜测)。

    测试通过 execute_high_risk_command_uow 触发 outbox_events UNIQUE 冲突,
    验证:
    1. 同 payload → 幂等(不抛错)
    2. 不同 payload → IDEMPOTENCY_CONFLICT
    3. 非 UNIQUE IntegrityError → 不被静默视为已排队
    """

    @pytest.mark.asyncio
    async def test_outbox_conflict_same_payload_idempotent(self, real_store):
        """UNIQUE 冲突 + 同 payload → 幂等(不抛错,事务正常提交)。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow,
        )
        action_id = "act_p1_2_idem"
        canonical_hash = "d" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )
        # 预先插入同 (a,e,t,rh) 的 outbox 事件(模拟上次成功提交但 worker 未完成)
        outbox_rh = "rh_p1_2_idem" + "0" * 54
        payload = json.dumps({"text": "purge done"})
        await real_store.add_outbox_event(
            action_id=action_id,
            effect_type="telegram_message",
            target="chat:notify",
            request_hash=outbox_rh,
            payload_json=payload,
        )
        await real_store._db.commit()

        async def _business_action(tx):
            return {"total_cleaned": 0}

        command = HighRiskCommand(
            action_id=action_id,
            command_type="data_lifecycle_break_glass",
            principal_id=1,
            request_hash=canonical_hash,
            owner="test_owner",
            effect_type="purge",
            effect_target=action_id,
            business_action=_business_action,
            outbox_events=[
                {
                    "effect_type": "telegram_message",
                    "target": "chat:notify",
                    "request_hash": outbox_rh,
                    "payload_json": payload,  # 同 payload
                }
            ],
        )
        grant = _make_grant(action_id, canonical_hash, future_iso, now)
        # 应该幂等成功(不抛错)
        result = await execute_high_risk_command_uow(command, grant)
        assert result.success is True
        # 验证 outbox_events 仍只有 1 条(未重复插入)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE action_id=?",
            (action_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1

    @pytest.mark.asyncio
    async def test_outbox_conflict_different_payload_raises_idempotency_conflict(
        self, real_store,
    ):
        """UNIQUE 冲突 + 不同 payload → IDEMPOTENCY_CONFLICT(不可盲目重试)。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow,
        )
        from services.error_codes import AppError, ErrorCodes
        action_id = "act_p1_2_conflict"
        canonical_hash = "e" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )
        # 预先插入同 (a,e,t,rh) 但 payload 不同的 outbox 事件
        outbox_rh = "rh_p1_2_conflict" + "0" * 51
        old_payload = json.dumps({"text": "old message"})
        await real_store.add_outbox_event(
            action_id=action_id,
            effect_type="telegram_message",
            target="chat:notify",
            request_hash=outbox_rh,
            payload_json=old_payload,
        )
        await real_store._db.commit()

        async def _business_action(tx):
            return {"total_cleaned": 0}

        new_payload = json.dumps({"text": "NEW message"})  # 不同 payload
        command = HighRiskCommand(
            action_id=action_id,
            command_type="data_lifecycle_break_glass",
            principal_id=1,
            request_hash=canonical_hash,
            owner="test_owner",
            effect_type="purge",
            effect_target=action_id,
            business_action=_business_action,
            outbox_events=[
                {
                    "effect_type": "telegram_message",
                    "target": "chat:notify",
                    "request_hash": outbox_rh,
                    "payload_json": new_payload,
                }
            ],
        )
        grant = _make_grant(action_id, canonical_hash, future_iso, now)
        # 应抛 IDEMPOTENCY_CONFLICT(sha256 digest 不匹配 → payload 被替换,不可盲目重试)
        with pytest.raises(AppError) as exc_info:
            await execute_high_risk_command_uow(command, grant)
        assert exc_info.value.code == ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT
        # safe_params 白名单仅暴露 action_id/effect_type/target(security:
        # digest_match / field_mismatch / reason 含内部状态,不暴露给用户)
        # 验证 digest 校验路径被触发:既有行的 payload 未被新 payload 覆盖
        cursor = await real_store._db.execute(
            "SELECT payload_json FROM outbox_events "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            (action_id, "telegram_message", "chat:notify", outbox_rh),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == old_payload  # 原始 payload 保留(未被子序列覆盖)
        assert row[0] != new_payload  # 新 payload 未被写入

    @pytest.mark.asyncio
    async def test_outbox_non_unique_integrity_error_not_silently_ignored(
        self, real_store, monkeypatch,
    ):
        """非 UNIQUE 的 IntegrityError(如 CHECK)→ 不被静默视为已排队。

        旧实现在 except 中通过 `str(err)` 含 "constraint" 子串判断"已排队",
        会吞掉 CHECK / NOT NULL / FK 错误。新实现仅处理 UNIQUE 冲突,
        其它 IntegrityError 抛错触发 ROLLBACK。
        """
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow,
        )
        from services.error_codes import AppError, ErrorCodes
        action_id = "act_p1_2_check"
        canonical_hash = "f" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )

        async def _business_action(tx):
            return {"total_cleaned": 0}

        outbox_rh = "rh_p1_2_check" + "0" * 54
        command = HighRiskCommand(
            action_id=action_id,
            command_type="data_lifecycle_break_glass",
            principal_id=1,
            request_hash=canonical_hash,
            owner="test_owner",
            effect_type="purge",
            effect_target=action_id,
            business_action=_business_action,
            outbox_events=[
                {
                    "effect_type": "telegram_message",
                    "target": "chat:notify",
                    "request_hash": outbox_rh,
                    "payload_json": "{}",
                }
            ],
        )
        grant = _make_grant(action_id, canonical_hash, future_iso, now)

        # monkey-patch store.add_outbox_event 抛出 CHECK constraint IntegrityError
        # (模拟 SQLite CHECK 约束失败,旧实现会因 "constraint" 子串而静默忽略)
        async def _mock_add_outbox_event(**kwargs):
            raise sqlite3.IntegrityError(
                "CHECK constraint failed: outbox_events.status"
            )

        monkeypatch.setattr(
            real_store, "add_outbox_event", _mock_add_outbox_event,
        )

        # 应抛错(不静默忽略),触发 ROLLBACK
        with pytest.raises(AppError) as exc_info:
            await execute_high_risk_command_uow(command, grant)
        # 错误码是 BREAK_GLASS_APPROVAL_REQUIRED(包装为 UoW 失败),
        # 而非被静默视为"已排队"
        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED
        # safe_params 白名单仅暴露 reason/approval_action_id(security:
        # reason 值因含完整异常消息长度 > 100 被 is_safe_param 二次过滤)
        # 验证非 UNIQUE IntegrityError 未被静默吞掉:
        # 1. 异常已传播(走到这里说明未被 except 静默)
        # 2. 原始 IntegrityError 在异常链中(__cause__)
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
        assert "CHECK constraint failed" in str(exc_info.value.__cause__)
        # 3. command_executions 未转到 executed(UoW ROLLBACK,状态机未推进)
        cursor = await real_store._db.execute(
            "SELECT status FROM command_executions WHERE action_id=?",
            (action_id,),
        )
        ce_row = await cursor.fetchone()
        assert ce_row is not None
        assert ce_row[0] != "executed"  # 未完成(ROLLBACK 生效)

    @pytest.mark.asyncio
    async def test_outbox_conflict_field_validation(self, real_store):
        """UNIQUE 冲突后逐字段验证 action/effect/target/hash(防御性校验)。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow,
        )
        action_id = "act_p1_2_fields"
        canonical_hash = "1" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )
        # 预先插入同 (a,e,t,rh) + 同 payload 的 outbox 事件
        outbox_rh = "rh_p1_2_fields" + "0" * 53
        payload = json.dumps({"text": "same"})
        await real_store.add_outbox_event(
            action_id=action_id,
            effect_type="telegram_message",
            target="chat:notify",
            request_hash=outbox_rh,
            payload_json=payload,
        )
        await real_store._db.commit()

        async def _business_action(tx):
            return {"total_cleaned": 0}

        command = HighRiskCommand(
            action_id=action_id,
            command_type="data_lifecycle_break_glass",
            principal_id=1,
            request_hash=canonical_hash,
            owner="test_owner",
            effect_type="purge",
            effect_target=action_id,
            business_action=_business_action,
            outbox_events=[
                {
                    "effect_type": "telegram_message",
                    "target": "chat:notify",
                    "request_hash": outbox_rh,
                    "payload_json": payload,  # 同 payload → 幂等
                }
            ],
        )
        grant = _make_grant(action_id, canonical_hash, future_iso, now)
        # 幂等成功(字段 + digest 一致)
        result = await execute_high_risk_command_uow(command, grant)
        assert result.success is True
        # 验证 outbox_events 仍只有 1 条(未重复)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE action_id=?",
            (action_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1

    @pytest.mark.asyncio
    async def test_outbox_operational_error_not_silently_ignored(
        self, real_store, monkeypatch,
    ):
        """OperationalError(如 disk full)→ 不被吞掉(非 IntegrityError)。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow,
        )
        from services.error_codes import AppError, ErrorCodes
        action_id = "act_p1_2_op"
        canonical_hash = "2" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )

        async def _business_action(tx):
            return {"total_cleaned": 0}

        outbox_rh = "rh_p1_2_op" + "0" * 56
        command = HighRiskCommand(
            action_id=action_id,
            command_type="data_lifecycle_break_glass",
            principal_id=1,
            request_hash=canonical_hash,
            owner="test_owner",
            effect_type="purge",
            effect_target=action_id,
            business_action=_business_action,
            outbox_events=[
                {
                    "effect_type": "telegram_message",
                    "target": "chat:notify",
                    "request_hash": outbox_rh,
                    "payload_json": "{}",
                }
            ],
        )
        grant = _make_grant(action_id, canonical_hash, future_iso, now)

        # monkey-patch 抛 OperationalError(非 IntegrityError,旧实现也会抛,
        # 但验证新实现不会因 "constraint" 子串误判)
        async def _mock_add_outbox_event(**kwargs):
            raise sqlite3.OperationalError("database is full")

        monkeypatch.setattr(
            real_store, "add_outbox_event", _mock_add_outbox_event,
        )

        with pytest.raises(AppError) as exc_info:
            await execute_high_risk_command_uow(command, grant)
        # 应被包装为 UoW 失败(外层 except Exception 包装),
        # 不应被静默视为"已排队"
        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED
        params_str = str(exc_info.value.params)
        assert "database is full" in params_str or "OperationalError" in params_str


# ════════════════════════════════════════════════════════════════
# P1-02: 直接测试 add_outbox_event UNIQUE 冲突的异常类型
# ════════════════════════════════════════════════════════════════

class TestAddOutboxEventUniqueExceptionType:
    """R63 P1-02: add_outbox_event UNIQUE 冲突抛 sqlite3.IntegrityError(可被 isinstance 识别)。"""

    @pytest.mark.asyncio
    async def test_unique_conflict_raises_sqlite3_integrity_error(self, real_store):
        """UNIQUE 冲突抛 sqlite3.IntegrityError(非泛 Exception)。

        这是 P1-02 整改的基础:UoW 通过 isinstance(err, sqlite3.IntegrityError)
        精确识别 UNIQUE 冲突,而非字符串猜测。
        """
        rh = "rh_unique_type" + "0" * 51
        # 第一次插入成功
        await real_store.add_outbox_event(
            action_id="act_unique_type",
            effect_type="telegram_message",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        # 第二次同 (a,e,t,rh) → 抛 sqlite3.IntegrityError
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            await real_store.add_outbox_event(
                action_id="act_unique_type",
                effect_type="telegram_message",
                target="chat:1",
                request_hash=rh,
                payload_json="{}",
            )
        # 验证错误消息含 UNIQUE + outbox_events
        err_str = str(exc_info.value)
        assert "UNIQUE constraint failed" in err_str
        assert "outbox_events" in err_str

    @pytest.mark.asyncio
    async def test_unique_conflict_error_message_format(self, real_store):
        """UNIQUE 冲突错误消息格式: 'UNIQUE constraint failed: outbox_events.col, ...'。"""
        rh = "rh_unique_fmt" + "0" * 51
        await real_store.add_outbox_event(
            action_id="act_unique_fmt",
            effect_type="telegram_message",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            await real_store.add_outbox_event(
                action_id="act_unique_fmt",
                effect_type="telegram_message",
                target="chat:1",
                request_hash=rh,
                payload_json="{}",
            )
        # 验证错误消息含所有 UNIQUE 列
        err_str = str(exc_info.value)
        for col in ("action_id", "effect_type", "target", "request_hash"):
            assert col in err_str
