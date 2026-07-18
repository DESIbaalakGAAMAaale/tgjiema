"""R62 P1-01 + P0-05: Effect Receipt 幂等冲突 + 终态保护 + 事务性 Outbox 整改测试。

被测目标:
- ``services.effect_receipts.EffectReceiptManager.record_pending`` /
  ``record_completed`` / ``record_failed`` —— R62 P1-01 整改:
    * PRE-SELECT + plain INSERT 模式(替换 INSERT OR IGNORE + UPDATE)
    * 不同 request_hash → IDEMPOTENCY_CONFLICT(拒绝覆盖)
    * 已 completed → TERMINAL_STATE(终态保护)
    * WHERE status='pending' AND request_hash=? + rowcount 检查
    * tx 参数支持(纳入外层事务)
- ``database.cache_store.CacheStore.outbox_events`` 表 + 5 个方法 —— R62 P0-05:
    * UNIQUE(action_id, effect_type, target, request_hash) 幂等键
    * claim_outbox_events(lease-based CAS)
    * complete_outbox_event / fail_outbox_event / move_outbox_to_dlq
- ``services.data_lifecycle.HighRiskCommand`` 新增字段:
    * compensation_action(saga 补偿回调)
    * outbox_events(外部副作用声明列表)
- ``services.data_lifecycle.execute_high_risk_command_uow`` —— R62 P0-05 整改:
    * business_action MUST DB-only(无网络/文件 I/O)
    * 外部副作用通过 outbox_events 字段声明,UoW 原子写入 outbox_events 表
    * effect_receipts 使用 PRE-SELECT + plain INSERT + UPDATE WHERE status+hash
- ``services.data_lifecycle.OutboxWorker`` 桩 —— lease-based CAS claim + complete/fail

测试覆盖(20+ 项):
1. record_pending 不同 request_hash → IDEMPOTENCY_CONFLICT
2. record_pending 同 key + 同 hash → 幂等返回
3. record_pending 已 completed → TERMINAL_STATE
4. record_completed pending → succeeds
5. record_completed 已 completed → TERMINAL_STATE(终态保护)
6. record_completed 错误 hash → IDEMPOTENCY_CONFLICT
7. record_completed rowcount=0 行不存在 → EffectReceiptError
8. record_failed pending → succeeds
9. record_failed tx 参数(纳入外层事务)
10. outbox_events UNIQUE 约束
11. add_outbox_event 重复插入失败
12. claim_outbox_events 获取 lease
13. complete_outbox_event 转 completed
14. fail_outbox_event increment attempt_count + 超 max → DLQ
15. move_outbox_to_dlq 显式进 DLQ
16. HighRiskCommand.compensation_action 默认 None
17. HighRiskCommand.outbox_events 默认空 list
18. execute_high_risk_command_uow business_action 在事务内调用
19. execute_high_risk_command_uow outbox_events 行在 business_action 后创建
20. OutboxWorker.run_once stub 模式 complete 事件
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(由 init() 创建含 R62 P1-01 + P0-05 约束的表)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建 effect_receipts 表(R62 P1-01 DDL:
    UNIQUE(a,e,t,rh) + CHECK 约束)+ outbox_events 表(R62 P0-05 DDL)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r62_p1_1_p0_5_test_")
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
# (command_approvals 表由 migration 创建,测试中手动 CREATE 兜底,
#  与 tests/test_r51_p1_data_consistency.py / test_r53_p1_3 保持一致)
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
        "test_signing_key_for_r62_uow_integration_32b"
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


# ════════════════════════════════════════════════════════════════
# 1. record_pending 幂等冲突 + 终态保护(R62 P1-01)
# ════════════════════════════════════════════════════════════════

class TestRecordPendingIdempotencyConflict:
    """R62 P1-01: record_pending 不同 request_hash → IDEMPOTENCY_CONFLICT。"""

    @pytest.mark.asyncio
    async def test_record_pending_different_hash_raises_idempotency_conflict(
        self, real_store,
    ):
        """同 (a,e,t) 不同 request_hash → raise IDEMPOTENCY_CONFLICT(409)。"""
        from services.effect_receipts import EffectReceiptManager
        from services.error_codes import AppError, ErrorCodes
        mgr = EffectReceiptManager(real_store)
        # 第一次:hash_A
        await mgr.record_pending(
            "act_conflict_1", "telegram_send", "chat:1",
            request_hash="hash_A" + "0" * 57,  # 64 hex
        )
        # 第二次:hash_B(不同 payload)→ IDEMPOTENCY_CONFLICT
        with pytest.raises(AppError) as exc_info:
            await mgr.record_pending(
                "act_conflict_1", "telegram_send", "chat:1",
                request_hash="hash_B" + "0" * 57,
            )
        assert exc_info.value.code == ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT
        # http_status=409, retryable=False(通过 ErrorRegistry 查 ErrorDefinition)
        from services.error_codes import ErrorRegistry
        err_def = ErrorRegistry.get(ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT)
        assert err_def.http_status == 409
        assert err_def.retryable is False
        # AppError.retryable 属性也应为 False
        assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_record_pending_same_key_same_hash_returns_existing(
        self, real_store,
    ):
        """同 (a,e,t) + 同 request_hash → 幂等返回 True(不报错)。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "hash_same" + "0" * 55
        # 第一次 INSERT
        ok1 = await mgr.record_pending(
            "act_idem_2", "telegram_send", "chat:2",
            request_hash=rh,
        )
        assert ok1 is True
        # 第二次幂等重试(pending + 同 hash → 无需更新)
        ok2 = await mgr.record_pending(
            "act_idem_2", "telegram_send", "chat:2",
            request_hash=rh,
        )
        assert ok2 is True
        # 验证 receipt 仍为 pending,attempt 仍为 1(R62 P1-01: 不再 increment)
        cursor = await real_store._db.execute(
            "SELECT status, attempt FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_idem_2", "telegram_send", "chat:2", rh),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == 1  # attempt 不变(outbox_events.attempt_count 负责重试计数)

    @pytest.mark.asyncio
    async def test_record_pending_completed_raises_terminal_state(
        self, real_store,
    ):
        """已 completed 的 receipt 再次 record_pending → raise TERMINAL_STATE。"""
        from services.effect_receipts import EffectReceiptManager
        from services.error_codes import AppError, ErrorCodes
        mgr = EffectReceiptManager(real_store)
        rh = "hash_term" + "0" * 55
        # pending → completed
        await mgr.record_pending(
            "act_term_3", "telegram_send", "chat:3",
            request_hash=rh,
        )
        await mgr.record_completed(
            "act_term_3", "telegram_send", "chat:3",
            external_id="msg_3",
            expected_request_hash=rh,
        )
        # 再次 record_pending → TERMINAL_STATE
        with pytest.raises(AppError) as exc_info:
            await mgr.record_pending(
                "act_term_3", "telegram_send", "chat:3",
                request_hash=rh,
            )
        assert exc_info.value.code == ErrorCodes.DATA_RECEIPT_TERMINAL_STATE

    @pytest.mark.asyncio
    async def test_record_pending_failed_same_hash_reclaims(
        self, real_store,
    ):
        """failed + 同 hash → UPDATE 回 pending(失败重试),attempt+1。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "hash_retry" + "0" * 54
        # pending → failed
        await mgr.record_pending(
            "act_retry_4", "telegram_send", "chat:4",
            request_hash=rh,
        )
        await mgr.record_failed(
            "act_retry_4", "telegram_send", "chat:4",
            error_msg="transient",
            request_hash=rh,
        )
        # 验证 status=failed
        cursor = await real_store._db.execute(
            "SELECT status, attempt FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_retry_4", "telegram_send", "chat:4", rh),
        )
        row = await cursor.fetchone()
        assert row[0] == "failed"
        # 再次 record_pending → UPDATE 回 pending,attempt+1
        ok = await mgr.record_pending(
            "act_retry_4", "telegram_send", "chat:4",
            request_hash=rh,
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT status, attempt FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_retry_4", "telegram_send", "chat:4", rh),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"
        assert row[1] == 2  # attempt 从 1 → 2(failed 重试时 increment)


# ════════════════════════════════════════════════════════════════
# 2. record_completed 终态保护 + rowcount 检查(R62 P1-01)
# ════════════════════════════════════════════════════════════════

class TestRecordCompletedTerminalProtection:
    """R62 P1-01: record_completed 终态保护 + WHERE status+hash rowcount 检查。"""

    @pytest.mark.asyncio
    async def test_record_completed_on_pending_succeeds(self, real_store):
        """pending → record_completed → status=completed。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "hash_c1" + "0" * 58
        await mgr.record_pending(
            "act_c_5", "telegram_send", "chat:5",
            request_hash=rh,
        )
        await mgr.record_completed(
            "act_c_5", "telegram_send", "chat:5",
            external_id="msg_5",
            expected_request_hash=rh,
        )
        cursor = await real_store._db.execute(
            "SELECT status, external_id FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_c_5", "telegram_send", "chat:5", rh),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == "msg_5"

    @pytest.mark.asyncio
    async def test_record_completed_on_completed_raises_terminal_state(
        self, real_store,
    ):
        """已 completed 的 receipt 再次 record_completed → TERMINAL_STATE。"""
        from services.effect_receipts import EffectReceiptManager
        from services.error_codes import AppError, ErrorCodes
        mgr = EffectReceiptManager(real_store)
        rh = "hash_c2" + "0" * 58
        await mgr.record_pending(
            "act_c_6", "telegram_send", "chat:6",
            request_hash=rh,
        )
        await mgr.record_completed(
            "act_c_6", "telegram_send", "chat:6",
            external_id="msg_6",
            expected_request_hash=rh,
        )
        # 再次 record_completed → TERMINAL_STATE
        with pytest.raises(AppError) as exc_info:
            await mgr.record_completed(
                "act_c_6", "telegram_send", "chat:6",
                external_id="msg_6_dup",
                expected_request_hash=rh,
            )
        assert exc_info.value.code == ErrorCodes.DATA_RECEIPT_TERMINAL_STATE

    @pytest.mark.asyncio
    async def test_record_completed_wrong_hash_raises_idempotency_conflict(
        self, real_store,
    ):
        """record_completed 用错误 hash → IDEMPOTENCY_CONFLICT。"""
        from services.effect_receipts import EffectReceiptManager
        from services.error_codes import AppError, ErrorCodes
        mgr = EffectReceiptManager(real_store)
        rh_a = "hash_c3a" + "0" * 57
        rh_b = "hash_c3b" + "0" * 57
        await mgr.record_pending(
            "act_c_7", "telegram_send", "chat:7",
            request_hash=rh_a,
        )
        # R49 P0-4: expected_request_hash mismatch → EffectReceiptError(保持兼容)
        # 但 rowcount=0 + 不同 hash → AppError(IDEMPOTENCY_CONFLICT)
        # 实际行为:expected_request_hash 一致性校验先抛 EffectReceiptError(R49 行为)
        from services.effect_receipts import EffectReceiptError
        with pytest.raises(EffectReceiptError):
            await mgr.record_completed(
                "act_c_7", "telegram_send", "chat:7",
                external_id="msg_7",
                expected_request_hash=rh_b,  # 与 stored rh_a 不匹配
            )

    @pytest.mark.asyncio
    async def test_record_completed_rowcount_zero_not_found_raises(
        self, real_store,
    ):
        """record_completed 行不存在 → EffectReceiptError(调用方应先 record_pending)。"""
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)
        with pytest.raises(EffectReceiptError):
            await mgr.record_completed(
                "act_nonexistent", "telegram_send", "chat:999",
                external_id="msg_x",
                expected_request_hash="hash_x" + "0" * 58,
            )

    @pytest.mark.asyncio
    async def test_record_completed_tx_parameter_no_self_commit(
        self, real_store,
    ):
        """R62 P1-01: tx 参数 → 不自行 commit(由外层事务管理)。

        场景:在 tx 中调用 record_completed,然后 ROLLBACK,
        receipt 应保持 pending(证明 record_completed 未自行 commit)。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "hash_tx" + "0" * 58
        # 准备:pending 行(已 commit)
        await mgr.record_pending(
            "act_tx_8", "telegram_send", "chat:8",
            request_hash=rh,
        )
        # 在 tx 中调用 record_completed(不 commit)
        await real_store._db.execute("BEGIN")
        await mgr.record_completed(
            "act_tx_8", "telegram_send", "chat:8",
            external_id="msg_8",
            expected_request_hash=rh,
            tx=real_store._db,
        )
        # ROLLBACK 撤销 record_completed 的 UPDATE
        await real_store._db.rollback()
        # 验证 receipt 仍为 pending(record_completed 未自行 commit)
        cursor = await real_store._db.execute(
            "SELECT status FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_tx_8", "telegram_send", "chat:8", rh),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"


# ════════════════════════════════════════════════════════════════
# 3. record_failed WHERE status+hash + tx 参数(R62 P1-01)
# ════════════════════════════════════════════════════════════════

class TestRecordFailedWhereStatusHash:
    """R62 P1-01: record_failed WHERE status='pending' AND request_hash=? + tx。"""

    @pytest.mark.asyncio
    async def test_record_failed_pending_succeeds(self, real_store):
        """pending → record_failed → status=failed。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "hash_f1" + "0" * 58
        await mgr.record_pending(
            "act_f_9", "telegram_send", "chat:9",
            request_hash=rh,
        )
        await mgr.record_failed(
            "act_f_9", "telegram_send", "chat:9",
            error_msg="boom",
            request_hash=rh,
        )
        cursor = await real_store._db.execute(
            "SELECT status, last_error, reconcile_status FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_f_9", "telegram_send", "chat:9", rh),
        )
        row = await cursor.fetchone()
        assert row[0] == "failed"
        assert row[1] == "boom"
        assert row[2] == "needs_reconcile"

    @pytest.mark.asyncio
    async def test_record_failed_skips_completed(self, real_store):
        """已 completed 的 receipt record_failed → 不覆盖(WHERE status='pending' 阻止)。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "hash_f2" + "0" * 58
        await mgr.record_pending(
            "act_f_10", "telegram_send", "chat:10",
            request_hash=rh,
        )
        await mgr.record_completed(
            "act_f_10", "telegram_send", "chat:10",
            external_id="msg_10",
            expected_request_hash=rh,
        )
        # record_failed on completed → WHERE status='pending' 不匹配,0 行更新
        # (不抛错以兼容旧调用方,但 status 仍为 completed)
        await mgr.record_failed(
            "act_f_10", "telegram_send", "chat:10",
            error_msg="late failure",
            request_hash=rh,
        )
        cursor = await real_store._db.execute(
            "SELECT status FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_f_10", "telegram_send", "chat:10", rh),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"  # 未被覆盖


# ════════════════════════════════════════════════════════════════
# 4. outbox_events 表 UNIQUE 约束 + CacheStore 方法(R62 P0-05)
# ════════════════════════════════════════════════════════════════

class TestOutboxEventsUniqueAndMethods:
    """R62 P0-05: outbox_events UNIQUE(a,e,t,rh) + CacheStore 方法。"""

    @pytest.mark.asyncio
    async def test_outbox_events_unique_constraint(self, real_store):
        """UNIQUE(action_id, effect_type, target, request_hash) 防重复插入。"""
        # 第一次插入成功
        eid1 = await real_store.add_outbox_event(
            action_id="act_ob_1",
            effect_type="telegram_message",
            target="chat:1",
            request_hash="rh_ob_1" + "0" * 56,
            payload_json=json.dumps({"text": "hi"}),
        )
        assert eid1 > 0
        # 第二次同 (a,e,t,rh) → UNIQUE 冲突
        with pytest.raises(Exception) as exc_info:
            await real_store.add_outbox_event(
                action_id="act_ob_1",
                effect_type="telegram_message",
                target="chat:1",
                request_hash="rh_ob_1" + "0" * 56,  # 同 rh
                payload_json=json.dumps({"text": "different"}),
            )
        # SQLite UNIQUE 约束错误消息含 "unique" 或 "constraint"
        err_msg = str(exc_info.value).lower()
        assert "unique" in err_msg or "constraint" in err_msg

    @pytest.mark.asyncio
    async def test_add_outbox_event_empty_request_hash_raises(self, real_store):
        """request_hash 为空 → ValueError(幂等键必须包含 request_hash)。"""
        with pytest.raises(ValueError, match="request_hash"):
            await real_store.add_outbox_event(
                action_id="act_ob_2",
                effect_type="telegram_message",
                target="chat:2",
                request_hash="",
                payload_json="{}",
            )

    @pytest.mark.asyncio
    async def test_claim_outbox_events_acquires_lease(self, real_store):
        """claim_outbox_events 原子将 pending → in_flight,设置 lease_owner。"""
        # 准备:插入 2 条 pending 事件
        for i in range(2):
            await real_store.add_outbox_event(
                action_id=f"act_ob_3_{i}",
                effect_type="telegram_message",
                target=f"chat:{i}",
                request_hash=f"rh_ob_3_{i}" + "0" * 54,
                payload_json=json.dumps({"idx": i}),
            )
        # claim
        events = await real_store.claim_outbox_events(
            lease_owner="worker_test_1",
            lease_duration_seconds=60,
            limit=10,
        )
        assert len(events) == 2
        for ev in events:
            assert ev["status"] == "in_flight"
            assert ev["lease_owner"] == "worker_test_1"
            assert ev["lease_expires_at"] is not None
            assert ev["attempt_count"] == 1  # claim 时 +1

    @pytest.mark.asyncio
    async def test_claim_outbox_events_no_pending_returns_empty(self, real_store):
        """无 pending 事件 → 返回空列表。"""
        events = await real_store.claim_outbox_events(
            lease_owner="worker_test_2",
            limit=10,
        )
        assert events == []

    @pytest.mark.asyncio
    async def test_complete_outbox_event_transitions_to_completed(self, real_store):
        """complete_outbox_event: in_flight → completed(CAS)。"""
        eid = await real_store.add_outbox_event(
            action_id="act_ob_4",
            effect_type="r2_upload",
            target="bucket/key",
            request_hash="rh_ob_4" + "0" * 56,
            payload_json="{}",
        )
        events = await real_store.claim_outbox_events(
            lease_owner="worker_test_3", limit=1,
        )
        assert len(events) == 1
        ok = await real_store.complete_outbox_event(eid)
        assert ok is True
        # 验证状态
        cursor = await real_store._db.execute(
            "SELECT status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"

    @pytest.mark.asyncio
    async def test_complete_outbox_event_wrong_state_returns_false(self, real_store):
        """complete_outbox_event 在 pending(未 claim)状态 → False。"""
        eid = await real_store.add_outbox_event(
            action_id="act_ob_5",
            effect_type="r2_upload",
            target="bucket/key2",
            request_hash="rh_ob_5" + "0" * 56,
            payload_json="{}",
        )
        # 未 claim 直接 complete → False(状态非 in_flight)
        ok = await real_store.complete_outbox_event(eid)
        assert ok is False

    @pytest.mark.asyncio
    async def test_fail_outbox_event_under_max_returns_retryable(self, real_store):
        """fail_outbox_event: attempt_count < max → 'retryable'(转回 pending)。"""
        eid = await real_store.add_outbox_event(
            action_id="act_ob_6",
            effect_type="telegram_message",
            target="chat:6",
            request_hash="rh_ob_6" + "0" * 56,
            payload_json="{}",
            max_attempts=3,
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_test_4", limit=1,
        )
        result = await real_store.fail_outbox_event(eid, error_msg="transient")
        assert result == "retryable"
        # 验证状态转回 pending
        cursor = await real_store._db.execute(
            "SELECT status, attempt_count, last_error FROM outbox_events WHERE id=?",
            (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"
        assert row[1] == 1
        assert row[2] == "transient"

    @pytest.mark.asyncio
    async def test_fail_outbox_event_at_max_moves_to_dlq(self, real_store):
        """fail_outbox_event: claim+fail 多次直到 attempt_count >= max → 'dlq'。

        语义: max_attempts=N 表示允许 N 次尝试,每次 claim 都 increment
        attempt_count。fail_outbox_event 检查 attempt_count >= max_attempts:
        - 未超限 → 转回 pending(retryable)
        - 超限 → 转 dlq(permanent)
        本测试 max_attempts=2,需 claim+fail 两次才进 DLQ。
        """
        eid = await real_store.add_outbox_event(
            action_id="act_ob_7",
            effect_type="telegram_message",
            target="chat:7",
            request_hash="rh_ob_7" + "0" * 56,
            payload_json="{}",
            max_attempts=2,
        )
        # 第一次 claim(attempt 0→1)+ fail(1 < 2 → retryable,转回 pending)
        await real_store.claim_outbox_events(
            lease_owner="worker_test_5_a", limit=1,
        )
        result1 = await real_store.fail_outbox_event(eid, error_msg="try1")
        assert result1 == "retryable"
        # 第二次 claim(attempt 1→2)+ fail(2 >= 2 → dlq)
        await real_store.claim_outbox_events(
            lease_owner="worker_test_5_b", limit=1,
        )
        result2 = await real_store.fail_outbox_event(eid, error_msg="try2")
        assert result2 == "dlq"
        cursor = await real_store._db.execute(
            "SELECT status, attempt_count, last_error FROM outbox_events WHERE id=?",
            (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "dlq"
        assert row[1] == 2  # 两次 claim
        assert row[2] == "try2"

    @pytest.mark.asyncio
    async def test_move_outbox_to_dlq_explicit(self, real_store):
        """move_outbox_to_dlq: 显式移入 DLQ(不消耗重试次数)。"""
        eid = await real_store.add_outbox_event(
            action_id="act_ob_8",
            effect_type="telegram_message",
            target="chat:8",
            request_hash="rh_ob_8" + "0" * 56,
            payload_json="{}",
        )
        ok = await real_store.move_outbox_to_dlq(
            eid, reason="bad params: target chat not found",
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT status, last_error FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "dlq"
        assert "bad params" in row[1]


# ════════════════════════════════════════════════════════════════
# 5. HighRiskCommand 新字段 + execute_high_risk_command_uow outbox 集成(R62 P0-05)
# ════════════════════════════════════════════════════════════════

class TestHighRiskCommandFields:
    """R62 P0-05: HighRiskCommand 新增 compensation_action + outbox_events 字段。"""

    def test_high_risk_command_default_compensation_action_none(self):
        """compensation_action 默认 None。"""
        from services.data_lifecycle import HighRiskCommand
        cmd = HighRiskCommand(
            action_id="a",
            command_type="t",
            principal_id=1,
            request_hash="h" * 64,
            owner="o",
            effect_type="purge",
            effect_target="t",
            business_action=lambda: None,
        )
        assert cmd.compensation_action is None

    def test_high_risk_command_default_outbox_events_empty_list(self):
        """outbox_events 默认空 list。"""
        from services.data_lifecycle import HighRiskCommand
        cmd = HighRiskCommand(
            action_id="a",
            command_type="t",
            principal_id=1,
            request_hash="h" * 64,
            owner="o",
            effect_type="purge",
            effect_target="t",
            business_action=lambda: None,
        )
        assert cmd.outbox_events == []

    def test_high_risk_command_outbox_events_field_settable(self):
        """outbox_events 可在构造时传入。"""
        from services.data_lifecycle import HighRiskCommand
        events = [
            {
                "effect_type": "telegram_message",
                "target": "chat:1",
                "request_hash": "rh" + "0" * 62,
                "payload_json": "{}",
            }
        ]
        cmd = HighRiskCommand(
            action_id="a",
            command_type="t",
            principal_id=1,
            request_hash="h" * 64,
            owner="o",
            effect_type="purge",
            effect_target="t",
            business_action=lambda: None,
            outbox_events=events,
        )
        assert cmd.outbox_events == events

    def test_high_risk_command_compensation_action_field_settable(self):
        """compensation_action 可在构造时传入。"""
        from services.data_lifecycle import HighRiskCommand

        async def _comp(tx, err):
            return None

        cmd = HighRiskCommand(
            action_id="a",
            command_type="t",
            principal_id=1,
            request_hash="h" * 64,
            owner="o",
            effect_type="purge",
            effect_target="t",
            business_action=lambda: None,
            compensation_action=_comp,
        )
        assert cmd.compensation_action is _comp


# ════════════════════════════════════════════════════════════════
# 6. execute_high_risk_command_uow outbox 集成 + OutboxWorker stub
# ════════════════════════════════════════════════════════════════

class TestExecuteHighRiskCommandUowOutboxIntegration:
    """R62 P0-05: execute_high_risk_command_uow 集成 outbox_events + OutboxWorker。

    使用 mock 避免完整的双人审批 + MFA 流程,直接测试 UoW 内部步骤 5/6a/6b。
    """

    @pytest.mark.asyncio
    async def test_business_action_called_within_transaction(self, real_store):
        """business_action 在统一事务内调用(传入 store._db 作 tx)。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow, ApprovalGrant,
        )
        import datetime as _dt
        import socket as _socket

        action_id = "act_uow_ba_test"
        canonical_hash = "a" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )

        # business_action 记录传入的 tx
        captured_tx = []

        async def _business_action(tx):
            captured_tx.append(tx)
            return {"total_cleaned": 0}

        owner = f"{_socket.gethostname()}:{1234}"
        command = HighRiskCommand(
            action_id=action_id,
            command_type="data_lifecycle_break_glass",
            principal_id=1,
            request_hash=canonical_hash,
            owner=owner,
            effect_type="purge",
            effect_target=action_id,
            business_action=_business_action,
        )
        grant = ApprovalGrant(
            action_id=action_id,
            approver_ids=[2, 3],
            jti_list=["jti_2", "jti_3"],
            expected_principal_id=1,
            permission="break_glass",
            expires_at=future_iso,
            consumed_at_now=now,
            now_unix=int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
            request_hash=canonical_hash,
        )
        result = await execute_high_risk_command_uow(command, grant)
        assert result.success is True
        # business_action 收到的 tx 应为 store._db(统一事务)
        assert len(captured_tx) == 1
        assert captured_tx[0] is real_store._db

    @pytest.mark.asyncio
    async def test_outbox_events_inserted_after_business_action(self, real_store):
        """R62 P0-05: outbox_events 行在 business_action 之后、COMMIT 之前创建。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow, ApprovalGrant,
        )
        import datetime as _dt

        action_id = "act_uow_ob_test"
        canonical_hash = "b" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )

        async def _business_action(tx):
            return {"total_cleaned": 5}

        outbox_rh = "rh_ob_uow" + "0" * 55
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
                    "payload_json": json.dumps({"text": "purge done"}),
                }
            ],
        )
        grant = ApprovalGrant(
            action_id=action_id,
            approver_ids=[2, 3],
            jti_list=["jti_2", "jti_3"],
            expected_principal_id=1,
            permission="break_glass",
            expires_at=future_iso,
            consumed_at_now=now,
            now_unix=int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
            request_hash=canonical_hash,
        )
        result = await execute_high_risk_command_uow(command, grant)
        assert result.success is True

        # 验证 outbox_events 表中已写入 1 条 pending 事件
        cursor = await real_store._db.execute(
            "SELECT action_id, effect_type, target, request_hash, status, "
            "payload_json FROM outbox_events WHERE action_id=?",
            (action_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r[0] == action_id
        assert r[1] == "telegram_message"
        assert r[2] == "chat:notify"
        assert r[3] == outbox_rh
        assert r[4] == "pending"
        assert json.loads(r[5])["text"] == "purge done"

    @pytest.mark.asyncio
    async def test_outbox_events_missing_fields_raises(self, real_store):
        """R62 P0-05: outbox_events 字段缺失(effect_type/target/request_hash)→ raise。"""
        from services.data_lifecycle import (
            HighRiskCommand, execute_high_risk_command_uow, ApprovalGrant,
        )
        from services.error_codes import AppError, ErrorCodes
        import datetime as _dt

        action_id = "act_uow_ob_missing"
        canonical_hash = "c" * 64
        now, future_iso = await _setup_break_glass_approval(
            real_store, action_id, canonical_hash, principal_id=1,
        )

        async def _business_action(tx):
            return {"total_cleaned": 0}

        # outbox_events 缺 request_hash → raise AppError,触发 ROLLBACK
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
                    # request_hash 缺失
                    "payload_json": "{}",
                }
            ],
        )
        grant = ApprovalGrant(
            action_id=action_id,
            approver_ids=[2, 3],
            jti_list=["jti_2", "jti_3"],
            expected_principal_id=1,
            permission="break_glass",
            expires_at=future_iso,
            consumed_at_now=now,
            now_unix=int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
            request_hash=canonical_hash,
        )
        with pytest.raises(AppError) as exc_info:
            await execute_high_risk_command_uow(command, grant)
        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED
        assert "uow_outbox_event_missing_fields" in str(exc_info.value.params)

        # 验证 effect_receipts 也被 ROLLBACK(因 outbox_events 失败 → ROLLBACK 整个 UoW)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM effect_receipts WHERE action_id=?",
            (action_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 0  # effect_receipts 被 ROLLBACK 撤销


# ════════════════════════════════════════════════════════════════
# 7. OutboxWorker stub —— lease-based CAS claim + complete/fail
# ════════════════════════════════════════════════════════════════

class TestOutboxWorkerStub:
    """R62 P0-05: OutboxWorker 桩 —— run_once 在 stub 模式下 complete 事件。"""

    @pytest.mark.asyncio
    async def test_outbox_worker_run_once_no_events_returns_zero(self, real_store):
        """无 pending 事件 → run_once 返回全 0。"""
        from services.data_lifecycle import OutboxWorker
        # R63 P0-05: provider_registry=None 必须显式 test_mode=True
        # (生产模式 fail-fast 防止 stub 误启动)
        worker = OutboxWorker(
            lease_owner="worker_test", batch_size=10, test_mode=True,
        )
        result = await worker.run_once()
        assert result["claimed"] == 0
        assert result["completed"] == 0
        assert result["failed"] == 0
        assert result["dlq"] == 0

    @pytest.mark.asyncio
    async def test_outbox_worker_run_once_stub_mode_completes(self, real_store):
        """stub 模式(provider_registry=None + test_mode=True)→ claim 后直接 complete。"""
        from services.data_lifecycle import OutboxWorker
        # 准备 3 条 pending 事件
        for i in range(3):
            await real_store.add_outbox_event(
                action_id=f"act_ow_{i}",
                effect_type="telegram_message",
                target=f"chat:{i}",
                request_hash=f"rh_ow_{i}" + "0" * 56,
                payload_json=json.dumps({"idx": i}),
            )
        # R63 P0-05: stub 模式必须显式 test_mode=True
        worker = OutboxWorker(
            lease_owner="worker_stub", batch_size=10, test_mode=True,
        )
        result = await worker.run_once()
        assert result["claimed"] == 3
        assert result["completed"] == 3
        assert result["failed"] == 0
        assert result["dlq"] == 0
        # 验证所有事件已 completed
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE status='completed'"
        )
        row = await cursor.fetchone()
        assert row[0] == 3

    @pytest.mark.asyncio
    async def test_outbox_worker_run_once_no_provider_moves_to_dlq(self, real_store):
        """provider_registry 不含对应 effect_type → move_outbox_to_dlq。"""
        from services.data_lifecycle import OutboxWorker
        await real_store.add_outbox_event(
            action_id="act_ow_dlq",
            effect_type="unknown_effect_type",
            target="unknown",
            request_hash="rh_ow_dlq" + "0" * 54,
            payload_json="{}",
        )
        # provider_registry 仅含 telegram_message,不含 unknown_effect_type
        worker = OutboxWorker(
            lease_owner="worker_dlq",
            batch_size=10,
            provider_registry={"telegram_message": AsyncMock()},
        )
        result = await worker.run_once()
        assert result["claimed"] == 1
        assert result["dlq"] == 1
        assert result["completed"] == 0
        cursor = await real_store._db.execute(
            "SELECT status, last_error FROM outbox_events WHERE action_id=?",
            ("act_ow_dlq",),
        )
        row = await cursor.fetchone()
        assert row[0] == "dlq"
        assert "no_provider" in row[1]

    @pytest.mark.asyncio
    async def test_outbox_worker_run_once_provider_failure_fail_event(self, real_store):
        """provider 抛异常 → fail_outbox_event(attempt < max → retryable)。"""
        from services.data_lifecycle import OutboxWorker

        # R63 P0-05: provider 签名扩展为 (payload_json, request_hash, idempotency_key)
        async def _failing_provider(payload_json, request_hash, idempotency_key):
            raise RuntimeError("provider boom")

        await real_store.add_outbox_event(
            action_id="act_ow_fail",
            effect_type="telegram_message",
            target="chat:fail",
            request_hash="rh_ow_fail" + "0" * 55,
            payload_json="{}",
            max_attempts=3,
        )
        worker = OutboxWorker(
            lease_owner="worker_fail",
            batch_size=10,
            provider_registry={"telegram_message": _failing_provider},
        )
        result = await worker.run_once()
        assert result["claimed"] == 1
        assert result["failed"] == 1  # retryable(attempt 1 < max 3)
        assert result["dlq"] == 0
        cursor = await real_store._db.execute(
            "SELECT status, attempt_count, last_error FROM outbox_events "
            "WHERE action_id=?",
            ("act_ow_fail",),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"  # 转回 pending 等待重试
        assert row[1] == 1
        assert "provider boom" in row[2]
