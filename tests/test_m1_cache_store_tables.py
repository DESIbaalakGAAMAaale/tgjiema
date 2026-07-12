"""M1 业务闭环测试 — cache_store.py 5 张新表 CRUD。

被测模块: ``database.cache_store.CacheStore`` 中 M1 新增的:
  - upload_sessions(上传会话状态机,7 个方法)
  - upload_outbox(事务发件箱,6 个方法)
  - quota_ledger(配额变更流水,3 个方法)
  - delivery_receipts(投递回执,5 个方法)
  - replication_tasks(副本复制任务,6 个方法)

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db),
  通过 monkeypatch 替换 ``database.cache_store.DB_PATH`` 指向临时路径。
- 每个 fixture 创建独立的 CacheStore 实例并 init(),结束后 close() + 清理临时目录。
- 验证状态机迁移、UNIQUE 约束(INSERT OR IGNORE/REPLACE)、边界条件。
- 幂等性测试: 重复插入同主键不报错,且不覆盖已有记录。

环境要求:
- ``database.cache_store.CacheStore`` 必须是真实类(非 conftest 降级注入的 MagicMock)。
  若被测环境缺少 aiosqlite 或 Python 版本不兼容 ``dict | None`` 语法,
  conftest 会用 MagicMock 占位 cache_store,本文件将整体 skip。
"""
import inspect
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
# conftest.py 在 cache_store 不可导入时会注入 MagicMock 占位,
# 此时 CacheStore 不是真实类(inspect.isclass → False),
# 整文件 skip 以避免误导(不报 FAILED)。
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ── Fixture ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例。

    隔离策略:
    1. 临时目录下的 test_cache.db(避免污染项目 data/cache_store.db)。
    2. monkeypatch 替换 ``database.cache_store.DB_PATH`` 模块属性。
    3. 结束后 close + shutil.rmtree。
    """
    tmpdir = tempfile.mkdtemp(prefix="m1_cache_test_")
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


# ── 辅助函数 ─────────────────────────────────────────────────────

def _make_upload_id(suffix="001"):
    return f"upload-{suffix}-{int(time.time() * 1000) % 1000000}"


def _make_outbox_id(suffix="001"):
    return f"outbox-{suffix}-{int(time.time() * 1000000) % 1000000}"


# ════════════════════════════════════════════════════════════════
# M1-1: upload_sessions 上传会话状态机
# ════════════════════════════════════════════════════════════════

class TestUploadSessions:
    """upload_sessions 表 CRUD 与状态机测试。

    状态机: RECEIVED → COPIED_PRIMARY → MANIFESTED →
            OPTIONS_PENDING → INDEX_PENDING → READY / ABORTED / EXPIRED
    """

    @pytest.mark.asyncio
    async def test_create_and_get_upload_session(self, store):
        """创建会话后按主键查询,返回 dict 且 status='RECEIVED'。"""
        upload_id = _make_upload_id("create")
        await store.create_upload_session(
            upload_id, user_id=1001,
            source_msg_ids=[10, 20, 30],
            options_json={"protect_content": True, "ttl": 3600},
            trace_id="trace-create-001",
        )

        session = await store.get_upload_session(upload_id)
        assert session is not None
        assert session["upload_id"] == upload_id
        assert session["user_id"] == 1001
        assert session["status"] == "RECEIVED"
        assert session["source_msg_ids"] == [10, 20, 30]
        assert session["options_json"] == {"protect_content": True, "ttl": 3600}
        assert session["trace_id"] == "trace-create-001"
        assert session["prev_status"] is None
        assert session["created_at"] > 0
        assert session["updated_at"] == session["created_at"]

    @pytest.mark.asyncio
    async def test_get_upload_session_not_found(self, store):
        """查询不存在的主键返回 None。"""
        result = await store.get_upload_session("nonexistent-upload-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_sessions_by_user(self, store):
        """活跃会话查询排除 READY/ABORTED/EXPIRED。"""
        # 准备:3 个活跃 + 1 个 READY + 1 个 ABORTED + 1 个 EXPIRED
        uid_active1 = _make_upload_id("a1")
        uid_active2 = _make_upload_id("a2")
        uid_active3 = _make_upload_id("a3")
        uid_ready = _make_upload_id("ready")
        uid_aborted = _make_upload_id("aborted")
        uid_expired = _make_upload_id("expired")
        for uid in (uid_active1, uid_active2, uid_active3, uid_ready, uid_aborted, uid_expired):
            await store.create_upload_session(uid, user_id=2002)

        await store.transition_upload_session(uid_ready, "READY")
        await store.transition_upload_session(uid_aborted, "ABORTED")
        await store.transition_upload_session(uid_expired, "EXPIRED")
        # 活跃中的也迁移一下,确保活跃状态在集合中
        await store.transition_upload_session(uid_active2, "COPIED_PRIMARY")
        await store.transition_upload_session(uid_active3, "MANIFESTED")

        actives = await store.get_active_upload_sessions_by_user(2002)
        active_ids = {s["upload_id"] for s in actives}
        # 3 个活跃会话应被返回
        assert uid_active1 in active_ids
        assert uid_active2 in active_ids
        assert uid_active3 in active_ids
        # 终态会话不应出现
        assert uid_ready not in active_ids
        assert uid_aborted not in active_ids
        assert uid_expired not in active_ids
        # 返回字段含 upload_id/user_id/status/created_at/updated_at
        first = actives[0]
        assert "upload_id" in first
        assert "user_id" in first
        assert "status" in first

    @pytest.mark.asyncio
    async def test_get_active_sessions_empty_user(self, store):
        """查询无任何会话的用户返回空列表。"""
        result = await store.get_active_upload_sessions_by_user(99999)
        assert result == []

    @pytest.mark.asyncio
    async def test_transition_session_status(self, store):
        """状态迁移成功:status/prev_status/transitioned_at 被更新,返回 True。"""
        upload_id = _make_upload_id("trans")
        await store.create_upload_session(upload_id, user_id=3003)

        ok = await store.transition_upload_session(upload_id, "COPIED_PRIMARY", reason="primary copied")
        assert ok is True

        session = await store.get_upload_session(upload_id)
        assert session["status"] == "COPIED_PRIMARY"
        assert session["prev_status"] == "RECEIVED"
        assert session["transition_reason"] == "primary copied"
        assert session["transitioned_at"] is not None
        assert session["updated_at"] >= session["created_at"]

    @pytest.mark.asyncio
    async def test_transition_same_status_noop(self, store):
        """迁移到与当前相同的状态:rowcount=0,返回 False。"""
        upload_id = _make_upload_id("noop")
        await store.create_upload_session(upload_id, user_id=4004)

        ok = await store.transition_upload_session(upload_id, "RECEIVED")
        assert ok is False

        session = await store.get_upload_session(upload_id)
        # 状态不变
        assert session["status"] == "RECEIVED"

    @pytest.mark.asyncio
    async def test_transition_with_update_fields(self, store):
        """迁移时通过 **update_fields 更新额外字段(primary_channel_id 等)。"""
        upload_id = _make_upload_id("fields")
        await store.create_upload_session(upload_id, user_id=5005)

        ok = await store.transition_upload_session(
            upload_id, "COPIED_PRIMARY",
            primary_channel_id=999,
            primary_msg_ids=[100, 200],
        )
        assert ok is True

        session = await store.get_upload_session(upload_id)
        assert session["primary_channel_id"] == 999
        assert session["primary_msg_ids"] == [100, 200]

    @pytest.mark.asyncio
    async def test_lease_session(self, store):
        """首次租约成功:lease_owner/lease_until 被设置。"""
        upload_id = _make_upload_id("lease")
        await store.create_upload_session(upload_id, user_id=6006)

        ok = await store.lease_upload_session(upload_id, "worker-A", lease_seconds=60)
        assert ok is True

        session = await store.get_upload_session(upload_id)
        assert session["lease_owner"] == "worker-A"
        assert session["lease_until"] is not None
        assert session["lease_until"] > time.time()

    @pytest.mark.asyncio
    async def test_lease_conflict(self, store):
        """租约冲突:已被其他 owner 持有且未过期时,新 owner 租约失败。"""
        upload_id = _make_upload_id("conflict")
        await store.create_upload_session(upload_id, user_id=7007)

        # worker-A 先租约 60 秒
        ok_a = await store.lease_upload_session(upload_id, "worker-A", lease_seconds=60)
        assert ok_a is True

        # worker-B 尝试租约(应失败,因为 A 的租约未过期)
        ok_b = await store.lease_upload_session(upload_id, "worker-B", lease_seconds=60)
        assert ok_b is False

        session = await store.get_upload_session(upload_id)
        assert session["lease_owner"] == "worker-A"

    @pytest.mark.asyncio
    async def test_lease_renewal_same_owner(self, store):
        """同一 owner 续租成功(lease_owner = owner 条件满足)。"""
        upload_id = _make_upload_id("renew")
        await store.create_upload_session(upload_id, user_id=8008)

        ok1 = await store.lease_upload_session(upload_id, "worker-X", lease_seconds=60)
        assert ok1 is True
        ok2 = await store.lease_upload_session(upload_id, "worker-X", lease_seconds=120)
        assert ok2 is True

        session = await store.get_upload_session(upload_id)
        assert session["lease_owner"] == "worker-X"

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions(self, store):
        """清理过期租约:lease_until < (now - ttl) 的活跃会话置为 EXPIRED。"""
        upload_id = _make_upload_id("exp")
        await store.create_upload_session(upload_id, user_id=9009)
        # 设置一个很早的 lease_until(1 小时前)
        await store.transition_upload_session(
            upload_id, "COPIED_PRIMARY",
            lease_owner="dead-worker",
            lease_until=time.time() - 3600,
        )

        # ttl_seconds=300:清理 lease_until < now-300 的会话
        cleaned = await store.cleanup_expired_upload_sessions(ttl_seconds=300)
        assert cleaned >= 1

        session = await store.get_upload_session(upload_id)
        assert session["status"] == "EXPIRED"
        assert session["prev_status"] == "COPIED_PRIMARY"

    @pytest.mark.asyncio
    async def test_cleanup_expired_skips_terminal(self, store):
        """清理不触及已终态(READY/ABORTED/EXPIRED)的会话。"""
        upload_id = _make_upload_id("noterm")
        await store.create_upload_session(upload_id, user_id=1010)
        await store.transition_upload_session(upload_id, "READY")
        # READY 不会被 cleanup 触及
        cleaned = await store.cleanup_expired_upload_sessions(ttl_seconds=300)
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_delete_session_wrong_status_fails(self, store):
        """删除处于非 READY/ABORTED/EXPIRED 状态的会话:返回 False。"""
        upload_id = _make_upload_id("delfail")
        await store.create_upload_session(upload_id, user_id=1111)
        # RECEIVED 状态不允许删除
        ok = await store.delete_upload_session(upload_id)
        assert ok is False

        session = await store.get_upload_session(upload_id)
        assert session is not None  # 仍存在

    @pytest.mark.asyncio
    async def test_delete_session_ready_ok(self, store):
        """READY 状态的会话可被删除,返回 True。"""
        upload_id = _make_upload_id("delok")
        await store.create_upload_session(upload_id, user_id=1212)
        await store.transition_upload_session(upload_id, "READY")

        ok = await store.delete_upload_session(upload_id)
        assert ok is True

        session = await store.get_upload_session(upload_id)
        assert session is None  # 已删除

    @pytest.mark.asyncio
    async def test_create_upload_session_idempotent(self, store):
        """同 upload_id 重复创建:INSERT OR IGNORE 不报错,不覆盖原记录。"""
        upload_id = _make_upload_id("idem")
        await store.create_upload_session(
            upload_id, user_id=1313, source_msg_ids=[1, 2],
        )
        # 再次创建(user_id 不同)
        await store.create_upload_session(
            upload_id, user_id=99999, source_msg_ids=[9, 9],
        )

        session = await store.get_upload_session(upload_id)
        # 原记录未被覆盖
        assert session["user_id"] == 1313
        assert session["source_msg_ids"] == [1, 2]


# ════════════════════════════════════════════════════════════════
# M1-2: upload_outbox 事务发件箱
# ════════════════════════════════════════════════════════════════

class TestUploadOutbox:
    """upload_outbox 表 CRUD 与状态机测试。

    状态机: PENDING → DISPATCHED → DONE / FAILED
    """

    @pytest.mark.asyncio
    async def test_create_and_get_pending_outbox(self, store):
        """创建发条后 get_pending_outbox 返回该条目,status='PENDING'。"""
        outbox_id = _make_outbox_id("create")
        upload_id = _make_upload_id("out-create")
        await store.create_outbox_entry(
            outbox_id, upload_id, code="CODE-001",
            target_user_id=2001, storage_channel_id=3001,
            storage_msg_ids=[10, 11], batch_file_meta=[{"id": "f1"}],
        )

        pending = await store.get_pending_outbox(limit=10)
        matched = [p for p in pending if p["outbox_id"] == outbox_id]
        assert len(matched) == 1
        entry = matched[0]
        assert entry["upload_id"] == upload_id
        assert entry["code"] == "CODE-001"
        assert entry["target_user_id"] == 2001
        assert entry["storage_channel_id"] == 3001
        assert entry["status"] == "PENDING"
        assert entry["storage_msg_ids"] == [10, 11]
        assert entry["batch_file_meta"] == [{"id": "f1"}]
        assert entry["attempts"] == 0

    @pytest.mark.asyncio
    async def test_mark_outbox_dispatched(self, store):
        """标记 PENDING → DISPATCHED:job_id/processed_at 被设置。"""
        outbox_id = _make_outbox_id("disp")
        await store.create_outbox_entry(
            outbox_id, _make_upload_id("out-disp"),
            code="C", target_user_id=1, storage_channel_id=2,
        )

        ok = await store.mark_outbox_dispatched(outbox_id, job_id=5001)
        assert ok is True

        pending = await store.get_pending_outbox(limit=10)
        # DISPATCHED 不再出现在 PENDING 列表
        assert all(p["outbox_id"] != outbox_id for p in pending)

    @pytest.mark.asyncio
    async def test_mark_outbox_done(self, store):
        """标记 DISPATCHED → DONE:返回 True。"""
        outbox_id = _make_outbox_id("done")
        await store.create_outbox_entry(
            outbox_id, _make_upload_id("out-done"),
            code="C", target_user_id=1, storage_channel_id=2,
        )
        await store.mark_outbox_dispatched(outbox_id, job_id=5002)

        ok = await store.mark_outbox_done(outbox_id)
        assert ok is True

    @pytest.mark.asyncio
    async def test_mark_outbox_done_from_pending(self, store):
        """PENDING → DONE 也合法(WHERE status IN ('PENDING','DISPATCHED'))。"""
        outbox_id = _make_outbox_id("pd")
        await store.create_outbox_entry(
            outbox_id, _make_upload_id("out-pd"),
            code="C", target_user_id=1, storage_channel_id=2,
        )
        ok = await store.mark_outbox_done(outbox_id)
        assert ok is True

    @pytest.mark.asyncio
    async def test_mark_outbox_failed(self, store):
        """标记失败:attempts+1,next_retry_at 被设置(status 不直接变 FAILED)。"""
        outbox_id = _make_outbox_id("fail")
        upload_id = _make_upload_id("out-fail")
        await store.create_outbox_entry(
            outbox_id, upload_id,
            code="C", target_user_id=1, storage_channel_id=2,
        )

        next_retry = time.time() + 60
        ok = await store.mark_outbox_failed(outbox_id, reason="network error", next_retry_at=next_retry)
        assert ok is True

        # 查询该条目(用存储的 upload_id)
        by_upload = await store.get_outbox_by_upload(upload_id)
        matched = [b for b in by_upload if b["outbox_id"] == outbox_id]
        assert matched
        entry = matched[0]
        assert entry["attempts"] == 1
        assert entry["next_retry_at"] == next_retry
        # status 仍为 PENDING(mark_outbox_failed 不直接置 FAILED)
        assert entry["status"] == "PENDING"

    @pytest.mark.asyncio
    async def test_get_outbox_by_upload(self, store):
        """按 upload_id 查询关联的所有发条。"""
        upload_id = _make_upload_id("multi")
        for i in range(3):
            await store.create_outbox_entry(
                _make_outbox_id(f"m{i}"), upload_id,
                code="C", target_user_id=1, storage_channel_id=2,
            )

        result = await store.get_outbox_by_upload(upload_id)
        assert len(result) == 3
        # 所有条目的 upload_id 一致
        for entry in result:
            assert entry["upload_id"] == upload_id

    @pytest.mark.asyncio
    async def test_pending_outbox_respects_next_retry(self, store):
        """get_pending_outbox 排除 next_retry_at 未到期的条目。"""
        # 条目1:next_retry_at 为未来时间 → 不出现
        oid1 = _make_outbox_id("future")
        await store.create_outbox_entry(
            oid1, _make_upload_id("out-fut"),
            code="C", target_user_id=1, storage_channel_id=2,
        )
        future = time.time() + 3600
        await store.mark_outbox_failed(oid1, reason="busy", next_retry_at=future)

        # 条目2:next_retry_at 已到期 → 出现
        oid2 = _make_outbox_id("past")
        await store.create_outbox_entry(
            oid2, _make_upload_id("out-past"),
            code="C", target_user_id=1, storage_channel_id=2,
        )
        past = time.time() - 10
        await store.mark_outbox_failed(oid2, reason="busy", next_retry_at=past)

        pending = await store.get_pending_outbox(limit=20)
        ids = [p["outbox_id"] for p in pending]
        assert oid2 in ids  # next_retry_at 已到期的应出现
        assert oid1 not in ids  # 未到期的应被排除

    @pytest.mark.asyncio
    async def test_outbox_status_transitions(self, store):
        """完整状态转换链:PENDING → DISPATCHED → DONE。"""
        oid = _make_outbox_id("chain")
        await store.create_outbox_entry(
            oid, _make_upload_id("out-chain"),
            code="C", target_user_id=1, storage_channel_id=2,
        )

        # DISPATCHED 后 DONE
        ok1 = await store.mark_outbox_dispatched(oid, job_id=7001)
        assert ok1 is True
        ok2 = await store.mark_outbox_done(oid)
        assert ok2 is True

        # 再 mark_done 应失败(状态已 DONE)
        ok3 = await store.mark_outbox_done(oid)
        assert ok3 is False

    @pytest.mark.asyncio
    async def test_create_outbox_idempotent(self, store):
        """同 outbox_id 重复创建:INSERT OR IGNORE 不报错。"""
        oid = _make_outbox_id("idem")
        upload_id = _make_upload_id("out-idem")
        await store.create_outbox_entry(
            oid, upload_id,
            code="ORIG", target_user_id=1, storage_channel_id=2,
        )
        # 重复创建(code 不同)
        await store.create_outbox_entry(
            oid, upload_id,
            code="DUPE", target_user_id=999, storage_channel_id=888,
        )

        by_upload = await store.get_outbox_by_upload(upload_id)
        matched = [b for b in by_upload if b["outbox_id"] == oid]
        assert len(matched) == 1
        # 原记录未被覆盖
        assert matched[0]["code"] == "ORIG"
        assert matched[0]["target_user_id"] == 1


# ════════════════════════════════════════════════════════════════
# M1-3: quota_ledger 配额变更流水
# ════════════════════════════════════════════════════════════════

class TestQuotaLedger:
    """quota_ledger 表 CRUD 测试(追加式日志,无 UPDATE/DELETE)。"""

    @pytest.mark.asyncio
    async def test_append_and_get_ledger(self, store):
        """追加流水后按 user_id 查询,默认倒序返回。"""
        await store.append_quota_ledger(
            user_id=3001, event_type="consume", is_external=0,
            quota_before=20, quota_after=19, request_id="req-001",
            reason="decode one file",
        )

        ledger = await store.get_quota_ledger(3001, limit=10)
        assert len(ledger) == 1
        entry = ledger[0]
        assert entry["user_id"] == 3001
        assert entry["event_type"] == "consume"
        assert entry["is_external"] == 0
        assert entry["quota_before"] == 20
        assert entry["quota_after"] == 19
        assert entry["request_id"] == "req-001"
        assert entry["reason"] == "decode one file"

    @pytest.mark.asyncio
    async def test_get_ledger_by_request(self, store):
        """按 request_id 查询流水(幂等检查)。"""
        await store.append_quota_ledger(
            user_id=3002, event_type="consume",
            quota_before=10, quota_after=9, request_id="req-unique-001",
        )
        await store.append_quota_ledger(
            user_id=3003, event_type="refund",
            quota_before=5, quota_after=6, request_id="req-unique-001",
        )

        result = await store.get_quota_ledger_by_request("req-unique-001")
        assert len(result) == 2
        events = {r["event_type"] for r in result}
        assert events == {"consume", "refund"}

    @pytest.mark.asyncio
    async def test_get_ledger_by_request_empty(self, store):
        """查询不存在的 request_id 返回空列表。"""
        result = await store.get_quota_ledger_by_request("nonexistent-req")
        assert result == []

    @pytest.mark.asyncio
    async def test_ledger_append_only(self, store):
        """追加式日志:多次 INSERT 都保留,不覆盖。"""
        for i in range(5):
            await store.append_quota_ledger(
                user_id=3004, event_type="consume",
                quota_before=10 - i, quota_after=9 - i,
                request_id=f"req-{i}",
            )

        ledger = await store.get_quota_ledger(3004, limit=100)
        assert len(ledger) == 5
        # 每条都有独立 ledger_id
        ids = {e["ledger_id"] for e in ledger}
        assert len(ids) == 5

    @pytest.mark.asyncio
    async def test_ledger_multiple_events(self, store):
        """多种事件类型(consume/refund/sync/reset/expire)都正确写入。"""
        events = ["consume", "refund", "sync", "reset", "expire"]
        for i, ev in enumerate(events):
            await store.append_quota_ledger(
                user_id=3005, event_type=ev,
                quota_before=i, quota_after=i + 1,
                request_id=f"req-multi-{i}",
            )

        ledger = await store.get_quota_ledger(3005, limit=100)
        assert len(ledger) == 5
        returned_events = {e["event_type"] for e in ledger}
        assert returned_events == set(events)

    @pytest.mark.asyncio
    async def test_ledger_limit(self, store):
        """limit 参数限制返回条数(倒序取最新 N 条)。"""
        for i in range(10):
            await store.append_quota_ledger(
                user_id=3006, event_type="consume",
                quota_before=i, quota_after=i,
                request_id=f"req-limit-{i}",
            )

        ledger = await store.get_quota_ledger(3006, limit=3)
        assert len(ledger) == 3
        # 倒序,应返回最后 3 条(req-limit-9, 8, 7)
        # 检查 ledger_id 是递增的(最新 = 最大 ID)
        ids = [e["ledger_id"] for e in ledger]
        assert ids == sorted(ids, reverse=True)


# ════════════════════════════════════════════════════════════════
# M1-4: delivery_receipts 投递回执
# ════════════════════════════════════════════════════════════════

class TestDeliveryReceipts:
    """delivery_receipts 表 CRUD 测试。

    状态: SENT → CONFIRMED / FAILED / PARTIAL
    UNIQUE: (job_id, source_msg_id)
    """

    @pytest.mark.asyncio
    async def test_upsert_and_get_receipts(self, store):
        """写入回执后按 job_id 查询,返回列表。"""
        await store.upsert_delivery_receipt(
            job_id=4001, source_msg_id=10001, target_user_id=5001,
            sent_msg_id=60001, status="SENT",
        )

        receipts = await store.get_delivery_receipts_by_job(4001)
        assert len(receipts) == 1
        r = receipts[0]
        assert r["job_id"] == 4001
        assert r["source_msg_id"] == 10001
        assert r["target_user_id"] == 5001
        assert r["sent_msg_id"] == 60001
        assert r["status"] == "SENT"

    @pytest.mark.asyncio
    async def test_upsert_replaces_existing(self, store):
        """INSERT OR REPLACE:同 (job_id, source_msg_id) 重复写入会替换旧记录。"""
        await store.upsert_delivery_receipt(
            job_id=4002, source_msg_id=10002, target_user_id=5002,
            sent_msg_id=60002, status="SENT",
        )
        # 再次写入(不同 sent_msg_id / status)
        await store.upsert_delivery_receipt(
            job_id=4002, source_msg_id=10002, target_user_id=5002,
            sent_msg_id=60099, status="SENT",
        )

        receipts = await store.get_delivery_receipts_by_job(4002)
        assert len(receipts) == 1  # 替换不增加行数
        assert receipts[0]["sent_msg_id"] == 60099

    @pytest.mark.asyncio
    async def test_confirm_receipt(self, store):
        """确认回执:status='CONFIRMED', confirmed_at 被设置。"""
        await store.upsert_delivery_receipt(
            job_id=4003, source_msg_id=10003, target_user_id=5003,
            sent_msg_id=60003, status="SENT",
        )

        ok = await store.confirm_delivery_receipt(4003, 10003, sent_msg_id=60003)
        assert ok is True

        receipts = await store.get_delivery_receipts_by_job(4003)
        assert receipts[0]["status"] == "CONFIRMED"
        assert receipts[0]["confirmed_at"] is not None

    @pytest.mark.asyncio
    async def test_confirm_receipt_nonexistent(self, store):
        """确认不存在的回执:返回 False。"""
        ok = await store.confirm_delivery_receipt(99999, 99999, sent_msg_id=99999)
        assert ok is False

    @pytest.mark.asyncio
    async def test_mark_delivery_failed(self, store):
        """标记失败:status='FAILED', attempts+1, error_reason 被设置。"""
        await store.upsert_delivery_receipt(
            job_id=4004, source_msg_id=10004, target_user_id=5004,
            sent_msg_id=60004, status="SENT",
        )

        ok = await store.mark_delivery_failed(4004, 10004, reason="flood wait")
        assert ok is True

        receipts = await store.get_delivery_receipts_by_job(4004)
        r = receipts[0]
        assert r["status"] == "FAILED"
        assert r["attempts"] == 1
        assert r["error_reason"] == "flood wait"

    @pytest.mark.asyncio
    async def test_get_sent_msg_ids(self, store):
        """查询 SENT/CONFIRMED 状态的 sent_msg_id 列表。"""
        # 写入 3 条:1 SENT + 1 CONFIRMED + 1 FAILED
        await store.upsert_delivery_receipt(
            4005, 10010, 5010, sent_msg_id=60010, status="SENT")
        await store.upsert_delivery_receipt(
            4005, 10011, 5011, sent_msg_id=60011, status="SENT")
        # 确认一条
        await store.confirm_delivery_receipt(4005, 10011, sent_msg_id=60011)
        # 第三条置为 FAILED
        await store.upsert_delivery_receipt(
            4005, 10012, 5012, sent_msg_id=60012, status="SENT")
        await store.mark_delivery_failed(4005, 10012, reason="err")

        ids = await store.get_sent_msg_ids_for_job(4005)
        # SENT 和 CONFIRMED 都应返回(60010, 60011),FAILED 不返回
        assert 60010 in ids
        assert 60011 in ids
        assert 60012 not in ids

    @pytest.mark.asyncio
    async def test_unique_constraint_job_source(self, store):
        """UNIQUE(job_id, source_msg_id) 约束:同组合只能有一条记录。"""
        await store.upsert_delivery_receipt(
            4006, 10020, 5020, sent_msg_id=60020, status="SENT")
        await store.upsert_delivery_receipt(
            4006, 10020, 5020, sent_msg_id=60021, status="SENT")

        receipts = await store.get_delivery_receipts_by_job(4006)
        assert len(receipts) == 1  # 仅一条
        assert receipts[0]["sent_msg_id"] == 60021  # 替换后的值

    @pytest.mark.asyncio
    async def test_receipt_status_transitions(self, store):
        """状态转换链:SENT → CONFIRMED 与 SENT → FAILED。"""
        # 路径1:SENT → CONFIRMED
        await store.upsert_delivery_receipt(
            4007, 10030, 5030, sent_msg_id=60030, status="SENT")
        ok1 = await store.confirm_delivery_receipt(4007, 10030, sent_msg_id=60030)
        assert ok1 is True
        r1 = (await store.get_delivery_receipts_by_job(4007))[0]
        assert r1["status"] == "CONFIRMED"

        # 路径2:SENT → FAILED
        await store.upsert_delivery_receipt(
            4008, 10031, 5031, sent_msg_id=60031, status="SENT")
        ok2 = await store.mark_delivery_failed(4008, 10031, reason="err")
        assert ok2 is True
        r2 = (await store.get_delivery_receipts_by_job(4008))[0]
        assert r2["status"] == "FAILED"


# ════════════════════════════════════════════════════════════════
# M1-5: replication_tasks 副本复制任务
# ════════════════════════════════════════════════════════════════

class TestReplicationTasks:
    """replication_tasks 表 CRUD 与状态机测试。

    状态机: PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED / FAILED
    UNIQUE: (group_id, file_unique_id, src_channel_id, dst_channel_id)
    """

    @pytest.mark.asyncio
    async def test_create_and_get_pending(self, store):
        """创建任务后 get_pending_replication_tasks 返回该任务。"""
        await store.create_replication_task(
            group_id=5001, file_unique_id="file-uniq-001",
            src_channel_id=7001, dst_channel_id=8001,
            src_msg_id=9001, priority=3,
        )

        pending = await store.get_pending_replication_tasks(limit=10, priority_max=10)
        matched = [p for p in pending if p["file_unique_id"] == "file-uniq-001"]
        assert len(matched) == 1
        t = matched[0]
        assert t["group_id"] == 5001
        assert t["src_channel_id"] == 7001
        assert t["dst_channel_id"] == 8001
        assert t["src_msg_id"] == 9001
        assert t["status"] == "PLANNED"
        assert t["priority"] == 3
        assert t["attempts"] == 0
        assert t["max_attempts"] == 3
        assert t["dst_msg_id"] is None

    @pytest.mark.asyncio
    async def test_create_duplicate_ignored(self, store):
        """INSERT OR IGNORE:同 UNIQUE 组合重复插入被忽略。"""
        await store.create_replication_task(
            group_id=5002, file_unique_id="dup-001",
            src_channel_id=7002, dst_channel_id=8002,
            src_msg_id=9002, priority=1,
        )
        # 重复(同 UNIQUE 键),不同 src_msg_id/priority
        await store.create_replication_task(
            group_id=5002, file_unique_id="dup-001",
            src_channel_id=7002, dst_channel_id=8002,
            src_msg_id=99999, priority=9,
        )

        pending = await store.get_pending_replication_tasks(limit=50, priority_max=10)
        matched = [p for p in pending if p["file_unique_id"] == "dup-001"]
        assert len(matched) == 1
        # 原记录未被覆盖
        assert matched[0]["src_msg_id"] == 9002
        assert matched[0]["priority"] == 1

    @pytest.mark.asyncio
    async def test_mark_copying(self, store):
        """PLANNED → COPYING:status/prev_status 被更新。"""
        await store.create_replication_task(
            group_id=5003, file_unique_id="cp-001",
            src_channel_id=7003, dst_channel_id=8003, src_msg_id=9003,
        )
        task = (await store.get_pending_replication_tasks(limit=50))[0]
        tid = task["task_id"]

        ok = await store.mark_replication_copying(tid)
        assert ok is True

        # 再次 mark_copying 应失败(状态已非 PLANNED)
        ok2 = await store.mark_replication_copying(tid)
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_mark_copied(self, store):
        """COPYING → COPIED_UNVERIFIED:dst_msg_id 被写入。"""
        await store.create_replication_task(
            group_id=5004, file_unique_id="cp-002",
            src_channel_id=7004, dst_channel_id=8004, src_msg_id=9004,
        )
        task = (await store.get_pending_replication_tasks(limit=50))[0]
        tid = task["task_id"]
        await store.mark_replication_copying(tid)

        ok = await store.mark_replication_copied(tid, dst_msg_id=9500)
        assert ok is True

        # 已不在 pending 列表(status != PLANNED)
        pending = await store.get_pending_replication_tasks(limit=50)
        assert all(p["task_id"] != tid for p in pending)

    @pytest.mark.asyncio
    async def test_mark_committed(self, store):
        """COPIED_UNVERIFIED → COMMITTED:committed_at 被写入。"""
        await store.create_replication_task(
            group_id=5005, file_unique_id="cp-003",
            src_channel_id=7005, dst_channel_id=8005, src_msg_id=9005,
        )
        task = (await store.get_pending_replication_tasks(limit=50))[0]
        tid = task["task_id"]
        await store.mark_replication_copying(tid)
        await store.mark_replication_copied(tid, dst_msg_id=9501)

        ok = await store.mark_replication_committed(tid)
        assert ok is True

    @pytest.mark.asyncio
    async def test_mark_committed_wrong_status_fails(self, store):
        """非 COPIED_UNVERIFIED 状态迁移到 COMMITTED 失败。"""
        await store.create_replication_task(
            group_id=5006, file_unique_id="cp-004",
            src_channel_id=7006, dst_channel_id=8006, src_msg_id=9006,
        )
        task = (await store.get_pending_replication_tasks(limit=50))[0]
        tid = task["task_id"]
        # 当前状态 PLANNED,直接 mark_committed 应失败
        ok = await store.mark_replication_committed(tid)
        assert ok is False

    @pytest.mark.asyncio
    async def test_mark_failed_under_max_attempts(self, store):
        """attempts < max_attempts 时:status 仍为 PLANNED,attempts+1。"""
        await store.create_replication_task(
            group_id=5007, file_unique_id="cp-005",
            src_channel_id=7007, dst_channel_id=8007, src_msg_id=9007,
        )
        task = (await store.get_pending_replication_tasks(limit=50))[0]
        tid = task["task_id"]

        # 第一次失败(max_attempts=3)
        ok = await store.mark_replication_failed(tid, reason="copy error", max_attempts=3)
        assert ok is True

        # 验证状态仍为 PLANNED(可重试)
        pending = await store.get_pending_replication_tasks(limit=50)
        matched = [p for p in pending if p["task_id"] == tid]
        assert matched
        assert matched[0]["attempts"] == 1
        assert matched[0]["status"] == "PLANNED"
        assert matched[0]["last_error"] == "copy error"
        assert matched[0]["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_mark_failed_exceeds_max_attempts(self, store):
        """attempts >= max_attempts 时:status='FAILED',next_retry_at=NULL。"""
        await store.create_replication_task(
            group_id=5008, file_unique_id="cp-006",
            src_channel_id=7008, dst_channel_id=8008, src_msg_id=9008,
        )
        task = (await store.get_pending_replication_tasks(limit=50))[0]
        tid = task["task_id"]

        # 连续失败 3 次(max_attempts=3)
        for i in range(3):
            ok = await store.mark_replication_failed(tid, reason=f"fail-{i}", max_attempts=3)
            assert ok is True

        # 验证:status=FAILED,不在 pending 列表
        pending = await store.get_pending_replication_tasks(limit=50)
        assert all(p["task_id"] != tid for p in pending)

        # 再次失败应仍为 FAILED(不会进一步变化)
        ok = await store.mark_replication_failed(tid, reason="extra fail", max_attempts=3)
        assert ok is True

    @pytest.mark.asyncio
    async def test_pending_orders_by_priority(self, store):
        """get_pending 按 priority ASC, created_at ASC 排序。"""
        # 创建多个不同优先级的任务
        for i, prio in enumerate([5, 1, 8, 3, 2]):
            await store.create_replication_task(
                group_id=5009 + i,
                file_unique_id=f"prio-{i}",
                src_channel_id=7100, dst_channel_id=8100,
                src_msg_id=9100 + i, priority=prio,
            )

        pending = await store.get_pending_replication_tasks(limit=10, priority_max=10)
        # 应按 priority ASC 排序
        priorities = [p["priority"] for p in pending]
        assert priorities == sorted(priorities)
        # priority=1 应在前 2 位
        assert pending[0]["priority"] == 1
        assert pending[1]["priority"] == 2

    @pytest.mark.asyncio
    async def test_pending_priority_max_filter(self, store):
        """priority_max 参数过滤掉高优先级数值(>priority_max)的任务。"""
        await store.create_replication_task(
            group_id=5010, file_unique_id="hi-prio",
            src_channel_id=7101, dst_channel_id=8101, src_msg_id=9101,
            priority=15,  # 超过 priority_max=10
        )
        await store.create_replication_task(
            group_id=5011, file_unique_id="lo-prio",
            src_channel_id=7102, dst_channel_id=8102, src_msg_id=9102,
            priority=2,
        )

        pending = await store.get_pending_replication_tasks(limit=10, priority_max=10)
        file_ids = [p["file_unique_id"] for p in pending]
        assert "lo-prio" in file_ids
        assert "hi-prio" not in file_ids  # priority=15 > 10 被过滤
