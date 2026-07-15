"""R53 P1-5: CommandBus 双状态机与 lease 恢复测试。

覆盖要点:
1. 高风险 action + requires_approval=1 + 调用 claim_execution() → 抛 AppError
2. 高风险 action + requires_approval=1 + 调用 claim_execution_approved() → 成功
3. 低风险 action + 调用 claim_execution() → 成功(不需要审批)
4. 高风险 action 不在 registry 中 → 警告(但不阻断)
5. lease 过期 → 状态转 retryable(不转 pending)
6. check_receipt_before_resume 验证 receipt 存在 → 恢复执行(跳过 handler)
7. check_receipt_before_resume 验证 receipt 不存在 → 重新执行副作用
8. HIGH_RISK_ACTIONS registry 完整性校验

设计要点:
- 使用真实 CacheStore(临时 db_path),保证 SQL CHECK 约束与 CAS UPDATE 行为真实
- 通过 monkeypatch 替换 command_bus._get_store 返回临时 store(避免污染全局单例)
- 每个测试用例独立(独立的 action_id + fixture 隔离)
- 中文注释,严格遵循现有测试风格
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# 确保项目根在 sys.path(直接运行 pytest 时也可导入)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.cache_store import CacheStore  # noqa: E402
from services import command_bus, effect_receipts  # noqa: E402
from services.command_bus import (  # noqa: E402
    CMD_STATUS_APPROVED,
    CMD_STATUS_EXECUTING,
    CMD_STATUS_PENDING,
    CMD_STATUS_RETRYABLE,
    HIGH_RISK_ACTIONS,
    check_receipt_before_resume,
    claim_execution,
    claim_execution_approved,
    cleanup_stale_leases,
)
from services.error_codes import AppError, ErrorCodes  # noqa: E402


# ════════════════════════════════════════════════════════════════
# 测试辅助函数
# ════════════════════════════════════════════════════════════════


async def _insert_execution(
    store: CacheStore,
    action_id: str,
    command_type: str,
    status: str,
    requires_approval: int = 0,
    approved_at: str | None = None,
    lease_until: str | None = None,
    owner: str | None = None,
) -> None:
    """辅助函数:插入一条 command_executions 记录(测试用)。"""
    now = command_bus._now_iso()
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        "request_hash, result, created_at, updated_at, requires_approval, approved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (
            action_id, command_type, 0, status, owner, lease_until,
            "test_request_hash", now, now, requires_approval, approved_at,
        ),
    )
    await store._db.commit()


async def _get_status(store: CacheStore, action_id: str) -> str | None:
    """辅助函数:查询 command_executions 当前状态。"""
    cursor = await store._db.execute(
        "SELECT status FROM command_executions WHERE action_id = ?",
        (action_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _get_field(store: CacheStore, action_id: str, field: str) -> object | None:
    """辅助函数:查询 command_executions 任意字段。"""
    # field 是测试内部固定字符串,不存在 SQL 注入风险
    cursor = await store._db.execute(
        f"SELECT {field} FROM command_executions WHERE action_id = ?",
        (action_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


# ════════════════════════════════════════════════════════════════
# pytest fixtures
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def real_store(tmp_path, monkeypatch):
    """构造真实 CacheStore 实例(临时 db_path),并 monkeypatch 到 command_bus._get_store。

    - 临时数据库文件隔离测试,避免污染生产/全局单例
    - init() 会执行所有 DDL(含 R53 P1-5 新增的 requires_approval 列与 CHECK 约束)
    - 同步初始化 EffectReceiptManager(供 check_receipt_before_resume 测试用)
    """
    db_path = tmp_path / "test_r53_p1_5_commandbus.db"
    store = CacheStore(db_path=str(db_path))
    await store.init()
    # monkeypatch command_bus._get_store 返回此 store(避免污染全局 _store 单例)
    monkeypatch.setattr(command_bus, "_get_store", lambda: store)
    # 初始化 EffectReceiptManager 单例(供 check_receipt_before_resume 调用)
    receipt_mgr = effect_receipts.EffectReceiptManager(store)
    monkeypatch.setattr(effect_receipts, "_receipt_manager", receipt_mgr)
    # 清空 command_executions / effect_receipts 表(防脏数据)
    await store._db.execute("DELETE FROM command_executions")
    await store._db.execute("DELETE FROM effect_receipts")
    await store._db.commit()
    yield store
    # 清理:关闭 db 连接
    if store._db:
        await store._db.close()


# ════════════════════════════════════════════════════════════════
# 测试用例
# ════════════════════════════════════════════════════════════════


def test_high_risk_actions_registry_complete():
    """R53 P1-5: 验证 HIGH_RISK_ACTIONS registry 包含所有要求的高风险动作。

    任务要求 registry 必须包含 12 个 action:
    ban / purge / takedown / restore / crdb_delete / r2_put / r2_download /
    telegram_copy / telegram_send / force_join / rotate / demote
    """
    expected = {
        "ban", "purge", "takedown", "restore",
        "crdb_delete", "r2_put", "r2_download",
        "telegram_copy", "telegram_send",
        "force_join", "rotate", "demote",
    }
    assert HIGH_RISK_ACTIONS == expected, (
        f"HIGH_RISK_ACTIONS registry 不匹配,期望 {expected},实际 {HIGH_RISK_ACTIONS}"
    )


def test_error_code_registered():
    """R53 P1-5: 验证 COMMAND_MUST_USE_APPROVAL_PATH 错误码已注册到 ErrorRegistry。"""
    from services.error_codes import ErrorRegistry
    assert ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH == "COMMAND.APPROVAL.MUST_USE_APPROVAL_PATH"
    assert ErrorRegistry.is_registered(ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH)
    definition = ErrorRegistry.get(ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH)
    assert definition.http_status == 403
    assert definition.severity == "critical"
    assert definition.retryable is False
    assert definition.message_key == "errors.command.approval.must_use_approval_path"


@pytest.mark.asyncio
async def test_high_risk_action_claim_execution_rejected(real_store):
    """R53 P1-5: 高风险 action + requires_approval=1 + 调用 claim_execution() → 抛 AppError。

    场景:command_type='ban' 在 HIGH_RISK_ACTIONS 中,requires_approval=1,
    误走旧 claim_execution 入口 → 必须 fail-closed 抛 AppError
    (COMMAND_MUST_USE_APPROVAL_PATH),且状态保持 pending 不变。
    """
    await _insert_execution(
        real_store, "action_high_risk_rejected", "ban",
        CMD_STATUS_PENDING, requires_approval=1,
    )
    with pytest.raises(AppError) as exc_info:
        await claim_execution("action_high_risk_rejected", "test_owner")
    # 错误码必须是 COMMAND_MUST_USE_APPROVAL_PATH
    assert exc_info.value.code == ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH
    # safe_params 应包含 action_id / command_type / reason
    assert exc_info.value.params.get("action_id") == "action_high_risk_rejected"
    assert exc_info.value.params.get("command_type") == "ban"
    assert "reason" in exc_info.value.params
    # 状态应保持 pending(未被 CAS UPDATE 修改)
    status = await _get_status(real_store, "action_high_risk_rejected")
    assert status == CMD_STATUS_PENDING


@pytest.mark.asyncio
async def test_high_risk_action_claim_execution_approved_success(real_store):
    """R53 P1-5: 高风险 action + requires_approval=1 + 调用 claim_execution_approved() → 成功。

    场景:command_type='ban' 在 HIGH_RISK_ACTIONS 中,requires_approval=1,
    走正确审批路径 claim_execution_approved(approved → executing)→ 成功 claim,
    且 approved_at 被同步写入(满足 SQL CHECK 约束)。
    """
    await _insert_execution(
        real_store, "action_high_risk_approved", "ban",
        CMD_STATUS_APPROVED, requires_approval=1,
    )
    claimed = await claim_execution_approved(
        "action_high_risk_approved", "test_owner", request_hash="test_request_hash",
    )
    assert claimed is True
    # 状态应为 executing
    status = await _get_status(real_store, "action_high_risk_approved")
    assert status == CMD_STATUS_EXECUTING
    # approved_at 应被同步写入(满足 SQL CHECK 约束)
    approved_at = await _get_field(real_store, "action_high_risk_approved", "approved_at")
    assert approved_at is not None
    assert approved_at != ""


@pytest.mark.asyncio
async def test_low_risk_action_claim_execution_success(real_store):
    """R53 P1-5: 低风险 action + 调用 claim_execution() → 成功(不需要审批)。

    场景:command_type='low_risk_action' 不在 HIGH_RISK_ACTIONS 中,
    requires_approval=0 → 走旧 claim_execution 入口 → 正常 CAS claim 成功。
    """
    await _insert_execution(
        real_store, "action_low_risk", "low_risk_action",
        CMD_STATUS_PENDING, requires_approval=0,
    )
    claimed = await claim_execution("action_low_risk", "test_owner")
    assert claimed is True
    # 状态应为 executing
    status = await _get_status(real_store, "action_low_risk")
    assert status == CMD_STATUS_EXECUTING


@pytest.mark.asyncio
async def test_requires_approval_but_not_in_high_risk_registry_warns(real_store):
    """R54 P0-1: requires_approval=1 一律禁止旧入口,fail-closed。

    场景:command_type='unknown_action' 不在 HIGH_RISK_ACTIONS 中,
    但 requires_approval=1 → claim_execution 必须 fail-closed 抛 AppError
    (COMMAND_MUST_USE_APPROVAL_PATH),无论 command_type 是否在 registry 中。

    R54 P0-1 整改:requires_approval=1 不再区分 registry 内外,
    未知 command_type 也必须走审批路径(fail-closed,防止 registry 漏项
    变成审批绕过)。
    """
    await _insert_execution(
        real_store, "action_unknown_high_risk", "unknown_action",
        CMD_STATUS_PENDING, requires_approval=1,
    )
    # R54 P0-1: requires_approval=1 一律抛 AppError,不再允许走旧入口
    with pytest.raises(AppError) as exc_info:
        await claim_execution("action_unknown_high_risk", "test_owner")
    assert exc_info.value.code == ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH
    # 验证 safe_params 包含 action_id / command_type / reason
    assert exc_info.value.params.get("action_id") == "action_unknown_high_risk"
    assert exc_info.value.params.get("command_type") == "unknown_action"
    assert "reason" in exc_info.value.params
    # 状态应保持 pending(未被 CAS UPDATE 修改)
    status = await _get_status(real_store, "action_unknown_high_risk")
    assert status == CMD_STATUS_PENDING


@pytest.mark.asyncio
async def test_lease_expiry_transitions_to_retryable(real_store):
    """R53 P1-5: lease 过期 → 状态转 retryable(不转 pending)。

    场景:status='executing' 且 lease_until < now(worker 崩溃后僵死)
    → cleanup_stale_leases 必须将状态转 'retryable'(不是 'pending'),
    防止其他 worker 通过 claim_execution 旧入口重新认领绕过审批。

    retryable 状态在 VALID_TRANSITIONS 中只能转 approved(重新审批后重试),
    保证高风险动作必须重新走审批路径。
    """
    # 插入一条 executing + lease_until < now 的记录(requires_approval=1 高风险)
    past_iso = (_dt.datetime.utcnow() - _dt.timedelta(seconds=60)).isoformat()
    await _insert_execution(
        real_store, "action_lease_expired", "restore",
        CMD_STATUS_EXECUTING, requires_approval=1,
        approved_at=command_bus._now_iso(),
        lease_until=past_iso, owner="dead_worker",
    )
    cleaned = await cleanup_stale_leases()
    assert cleaned == 1
    # 高风险动作状态必须是 retryable(不是 pending,需重新审批)
    status = await _get_status(real_store, "action_lease_expired")
    assert status == CMD_STATUS_RETRYABLE, (
        f"高风险 lease 过期应转 retryable,实际转 {status}(必须不可直接转 pending)"
    )
    assert status != CMD_STATUS_PENDING


@pytest.mark.asyncio
async def test_check_receipt_before_resume_receipt_exists(real_store):
    """R53 P1-5: check_receipt_before_resume 验证 receipt 存在 → 恢复执行(跳过 handler)。

    场景:effect_receipts 表中已有 status='completed' 的 receipt
    → check_receipt_before_resume 返回 resume=False,
    调用方应跳过 handler(外部副作用已执行,避免重复)。
    """
    action_id = "action_receipt_exists"
    effect_type = "telegram_send"
    target = "send_message"
    now = command_bus._now_iso()
    # 插入一条 completed receipt
    await real_store._db.execute(
        "INSERT INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, created_at, "
        "completed_at, request_hash, attempt, lease_owner, lease_until, "
        "last_error, reconcile_status) "
        "VALUES (?, ?, ?, 'completed', 'msg_id_123', ?, ?, 'hash_abc', 1, "
        "NULL, NULL, NULL, 'completed')",
        (action_id, effect_type, target, now, now),
    )
    await real_store._db.commit()
    result = await check_receipt_before_resume(action_id, effect_type, target)
    # 应跳过 handler(resume=False)
    assert result["resume"] is False
    assert result["reason"] == "receipt_completed"
    assert result["external_id"] == "msg_id_123"
    # receipt 字段应包含完整记录
    assert "receipt" in result
    assert result["receipt"]["status"] == "completed"


@pytest.mark.asyncio
async def test_check_receipt_before_resume_receipt_missing(real_store):
    """R53 P1-5: check_receipt_before_resume 验证 receipt 不存在 → 重新执行副作用。

    场景:effect_receipts 表中无对应 receipt(或非 completed)
    → check_receipt_before_resume 返回 resume=True,
    调用方应重新执行 handler(外部副作用尚未完成)。
    """
    result = await check_receipt_before_resume(
        "action_no_receipt", "telegram_send", "send_message",
    )
    # 应重新执行 handler(resume=True)
    assert result["resume"] is True
    assert result["reason"] == "no_completed_receipt"


@pytest.mark.asyncio
async def test_check_receipt_before_resume_receipt_pending(real_store):
    """R53 P1-5: 补充测试 — receipt 存在但 status='pending'(未完成)→ 重新执行。

    场景:effect_receipts 表中有 status='pending' 的 receipt(上次执行未完成)
    → check_receipt_before_resume 返回 resume=True,
    调用方应重新执行 handler(副作用未完成,需重试)。
    """
    action_id = "action_receipt_pending"
    effect_type = "r2_put"
    target = "upload_file"
    now = command_bus._now_iso()
    # 插入一条 pending receipt(尚未完成)
    await real_store._db.execute(
        "INSERT INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, created_at, "
        "completed_at, request_hash, attempt, lease_owner, lease_until, "
        "last_error, reconcile_status) "
        "VALUES (?, ?, ?, 'pending', NULL, ?, NULL, 'hash_def', 1, "
        "'worker_1', ?, NULL, NULL)",
        (action_id, effect_type, target, now, now),
    )
    await real_store._db.commit()
    result = await check_receipt_before_resume(action_id, effect_type, target)
    # 应重新执行 handler(resume=True,因为 receipt 非 completed)
    assert result["resume"] is True
    assert result["reason"] == "no_completed_receipt"


@pytest.mark.asyncio
async def test_check_constraint_blocks_high_risk_executing_without_approved_at(real_store):
    """R53 P1-5: SQL CHECK 约束兜底 — 高风险动作 status='executing' 必须有 approved_at。

    场景:直接通过 SQL INSERT 一条 requires_approval=1 + status='executing'
    + approved_at=NULL 的记录 → 应被 CHECK 约束拒绝(sqlite3.IntegrityError)。

    这验证 DDL CHECK 约束 (requires_approval=0 OR status!='executing'
    OR approved_at IS NOT NULL) 真实生效(对新表)。
    """
    import sqlite3
    now = command_bus._now_iso()
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        await real_store._db.execute(
            "INSERT INTO command_executions "
            "(action_id, command_type, principal_id, status, owner, lease_until, "
            "request_hash, result, created_at, updated_at, requires_approval, approved_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, NULL)",
            ("action_check_violation", "ban", 0, CMD_STATUS_EXECUTING,
             "test_hash", now, now, 1),
        )
    # 错误信息应包含 CHECK constraint
    assert "CHECK" in str(exc_info.value).upper() or "CONSTRAINT" in str(exc_info.value).upper()


@pytest.mark.asyncio
async def test_check_constraint_allows_low_risk_executing_without_approved_at(real_store):
    """R53 P1-5: SQL CHECK 约束兜底 — 低风险动作 status='executing' 可无 approved_at。

    场景:requires_approval=0 + status='executing' + approved_at=NULL
    → CHECK 约束应放行(requires_approval=0 短路)。
    """
    now = command_bus._now_iso()
    # 不应抛异常
    await real_store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        "request_hash, result, created_at, updated_at, requires_approval, approved_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, NULL)",
        ("action_low_risk_executing", "low_risk_action", 0, CMD_STATUS_EXECUTING,
         "test_hash", now, now, 0),
    )
    await real_store._db.commit()
    # 验证记录已写入
    status = await _get_status(real_store, "action_low_risk_executing")
    assert status == CMD_STATUS_EXECUTING
