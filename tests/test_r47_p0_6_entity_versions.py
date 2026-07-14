"""R47 P0-6 / P0-5 / P1-a / P1-b 整改测试。

测试覆盖:
1. entity_versions 接入 add_dirty_outbox — version=0 自动调用 allocate_version
2. 多连接并发属性测试 — version 严格递增且无重复
3. dirty_outbox UNIQUE(table_name, pk, version) 约束 + 冲突重试
4. delivery_group_receipts 表 CRUD(P0-5)
5. callback_nonces 表原子消费(P1-a)
6. mfa_failures 新 schema(id AUTOINCREMENT + failed_at_ms)(P1-b)

测试策略:
- 真实 SQLite 临时文件数据库(隔离生产数据)
- 多连接并发测试用 asyncio.gather 模拟并发写入
- 不依赖 mock,验证真实 SQLite 行为
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# 测试环境兼容
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite cache_store
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(R47 测试用)。"""
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r47_p0_6_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = cs_module.DB_PATH
    original_store = getattr(cs_module, "_store", None)
    cs_module.DB_PATH = db_path
    try:
        s = cs_module.CacheStore()
        await s.init()
        cs_module._store = s
        yield s
        await s.close()
    finally:
        cs_module.DB_PATH = original_path
        if original_store is not None:
            cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 1. entity_versions 接入 add_dirty_outbox
# ════════════════════════════════════════════════════════════════

class TestEntityVersionsIntegration:
    """R47 P0-6: add_dirty_outbox version=0 通过 allocate_version 分配。"""

    @pytest.mark.asyncio
    async def test_version_zero_uses_allocate_version(self, cache_store):
        """version=0 时应调用 allocate_version,返回递增整数(非时间戳)。"""
        store = cache_store
        rid = await store.add_dirty_outbox(
            "users", "user-int-001", "upsert",
            payload=json.dumps({"user_id": 1}),
            version=0,
        )
        assert rid > 0
        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id = ?", (rid,)
        )
        row = await cursor.fetchone()
        assert row is not None
        # 首次调用应返回 1(allocate_version 从 1 开始)
        assert row[0] == 1

    @pytest.mark.asyncio
    async def test_version_zero_monotonic_increment(self, cache_store):
        """同 (table, pk) 多次 version=0 应产生严格递增的 version。"""
        store = cache_store
        versions = []
        for i in range(5):
            rid = await store.add_dirty_outbox(
                "users", "user-int-002", "upsert",
                payload=json.dumps({"i": i}),
                version=0,
            )
            cursor = await store._db.execute(
                "SELECT version FROM dirty_outbox WHERE id = ?", (rid,)
            )
            row = await cursor.fetchone()
            versions.append(row[0])
        # 应为 [1, 2, 3, 4, 5]
        assert versions == [1, 2, 3, 4, 5], (
            f"version 应严格递增 [1,2,3,4,5],实际 {versions}"
        )

    @pytest.mark.asyncio
    async def test_explicit_version_bypasses_allocate(self, cache_store):
        """显式 version > 0 时不调用 allocate_version,直接使用显式值。"""
        store = cache_store
        rid = await store.add_dirty_outbox(
            "users", "user-int-003", "upsert",
            payload=json.dumps({"user_id": 1}),
            version=42,
        )
        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id = ?", (rid,)
        )
        row = await cursor.fetchone()
        assert row[0] == 42

    @pytest.mark.asyncio
    async def test_different_pks_independent_version(self, cache_store):
        """不同 pk 的 version 应独立分配(互不影响)。"""
        store = cache_store
        # user-A 第一次 → version=1
        rid_a1 = await store.add_dirty_outbox(
            "users", "user-A", "upsert", version=0,
        )
        # user-B 第一次 → version=1(独立计数)
        rid_b1 = await store.add_dirty_outbox(
            "users", "user-B", "upsert", version=0,
        )
        # user-A 第二次 → version=2
        rid_a2 = await store.add_dirty_outbox(
            "users", "user-A", "upsert", version=0,
        )
        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id IN (?, ?, ?) ORDER BY id",
            (rid_a1, rid_b1, rid_a2),
        )
        rows = await cursor.fetchall()
        assert [r[0] for r in rows] == [1, 1, 2], (
            f"不同 pk version 应独立,期望 [1,1,2],实际 {[r[0] for r in rows]}"
        )


# ════════════════════════════════════════════════════════════════
# 2. 多连接并发属性测试 — version 严格递增且无重复
# ════════════════════════════════════════════════════════════════

class TestConcurrentVersionAllocation:
    """R47 P0-6: 多连接并发 allocate_version 保证严格递增且无重复。"""

    @pytest.mark.asyncio
    async def test_concurrent_allocate_version_no_duplicates(self, cache_store):
        """并发调用 allocate_version 应产生无重复的递增 version 序列。"""
        store = cache_store
        # 并发调用 allocate_version 20 次
        _N = 20
        results = await asyncio.gather(*[
            store.allocate_version("users", "user-concurrent-001")
            for _ in range(_N)
        ])
        # 验证: 无重复 + 范围 [1, _N]
        assert len(results) == _N
        assert len(set(results)) == _N, (
            f"并发分配的 version 有重复: {results}"
        )
        assert min(results) == 1
        assert max(results) == _N

    @pytest.mark.asyncio
    async def test_concurrent_add_dirty_outbox_no_version_collision(
        self, cache_store,
    ):
        """并发 add_dirty_outbox(version=0) 应无 UNIQUE 冲突(version 各不相同)。"""
        store = cache_store
        _N = 15
        # 并发写入同 (table, pk) 的 dirty_outbox,version=0 自动分配
        rids = await asyncio.gather(*[
            store.add_dirty_outbox(
                "users", "user-concurrent-002", "upsert",
                payload=json.dumps({"i": i}),
                version=0,
            )
            for i in range(_N)
        ])
        assert len(rids) == _N
        # 查询所有写入的 version
        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox "
            "WHERE table_name = 'users' AND pk = 'user-concurrent-002' "
            "ORDER BY version",
        )
        rows = await cursor.fetchall()
        versions = [r[0] for r in rows]
        # 应有 _N 条记录,version 严格递增 [1, 2, ..., _N]
        assert len(versions) == _N, (
            f"应有 {_N} 条记录,实际 {len(versions)}"
        )
        assert len(set(versions)) == _N, (
            f"version 有重复: {versions}"
        )
        assert versions == list(range(1, _N + 1)), (
            f"version 应为 [1..{_N}],实际 {versions}"
        )

    @pytest.mark.asyncio
    async def test_allocate_version_with_connection_param(self, cache_store):
        """allocate_version 接受 connection 参数,在指定事务连接上执行。"""
        store = cache_store
        # 使用 transaction 上下文
        async with store.transaction() as tx:
            v1 = await store.allocate_version("users", "user-tx-001", connection=tx)
            v2 = await store.allocate_version("users", "user-tx-001", connection=tx)
        assert v1 == 1
        assert v2 == 2


# ════════════════════════════════════════════════════════════════
# 3. dirty_outbox UNIQUE 约束 + 冲突重试
# ════════════════════════════════════════════════════════════════

class TestDirtyOutboxUniqueConstraint:
    """R47 P0-6: dirty_outbox UNIQUE(table_name, pk, version) 约束。"""

    @pytest.mark.asyncio
    async def test_unique_constraint_rejects_duplicate(self, cache_store):
        """直接 INSERT 重复 (table, pk, version) 应抛出 IntegrityError。"""
        store = cache_store
        # 先写入一条
        await store._db.execute(
            "INSERT INTO dirty_outbox (table_name, pk, version, operation, payload, "
            "created_at, processed, local_only) VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
            ("users", "user-uniq", 100, "upsert", "{}", "2026-07-14T00:00:00"),
        )
        await store._db.commit()
        # 再写入相同 (table, pk, version) 应失败
        with pytest.raises(sqlite3.IntegrityError):
            await store._db.execute(
                "INSERT INTO dirty_outbox (table_name, pk, version, operation, payload, "
                "created_at, processed, local_only) VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                ("users", "user-uniq", 100, "upsert", "{}", "2026-07-14T01:00:00"),
            )
            await store._db.commit()
        await store._db.rollback()

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_retry_on_unique_conflict(self, cache_store):
        """显式 version 冲突时,add_dirty_outbox 应自动重试分配新 version。"""
        store = cache_store
        # 先用显式 version=1 写入
        rid1 = await store.add_dirty_outbox(
            "users", "user-retry", "upsert",
            payload=json.dumps({"i": 1}),
            version=1,
        )
        assert rid1 > 0
        # 再用相同 version=1 写入 — 应触发 UNIQUE 冲突并重试
        # 重试时调用 allocate_version,分配到 2(entity_versions 已有 1 条记录
        # 由于第一次显式 version=1 未调用 allocate_version,entity_versions 无记录,
        # 重试时 allocate_version 返回 1 → 仍冲突 → 再返回 2)
        rid2 = await store.add_dirty_outbox(
            "users", "user-retry", "upsert",
            payload=json.dumps({"i": 2}),
            version=1,  # 冲突,应自动重试
        )
        assert rid2 > 0
        # 查询第二条记录的 version(应为 allocate_version 分配的值)
        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id = ?", (rid2,)
        )
        row = await cursor.fetchone()
        # version 应 != 1(因为 1 已被占用,重试分配新值)
        assert row[0] != 1, (
            f"UNIQUE 冲突后应重试分配新 version,实际仍为 1"
        )


# ════════════════════════════════════════════════════════════════
# 4. delivery_group_receipts CRUD(P0-5)
# ════════════════════════════════════════════════════════════════

class TestDeliveryGroupReceipts:
    """R47 P0-5: delivery_group_receipts 群发回执 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_create_and_get_receipt(self, cache_store):
        """创建回执后按 group_id 查询,返回正确字段。"""
        store = cache_store
        ok = await store.delivery_group_receipt_create(
            "group-001", expected_count=3,
            source_ids=[10, 20, 30], target_ids=[1001, 1002, 1003],
            action_id="action-001",
        )
        assert ok is True
        receipt = await store.delivery_group_receipt_get("group-001")
        assert receipt is not None
        assert receipt["group_id"] == "group-001"
        assert receipt["expected_count"] == 3
        assert receipt["confirmed_count"] == 0
        assert receipt["status"] == "pending"
        assert receipt["source_ids"] == [10, 20, 30]
        assert receipt["target_ids"] == [1001, 1002, 1003]
        assert receipt["action_id"] == "action-001"

    @pytest.mark.asyncio
    async def test_create_idempotent(self, cache_store):
        """重复创建相同 group_id 应幂等(不报错,不覆盖)。"""
        store = cache_store
        await store.delivery_group_receipt_create(
            "group-002", expected_count=5,
            source_ids=[1], target_ids=[2],
            action_id="action-002",
        )
        # 再次创建(不同参数)应被 IGNORE
        await store.delivery_group_receipt_create(
            "group-002", expected_count=99,
            source_ids=[999], target_ids=[999],
            action_id="action-different",
        )
        receipt = await store.delivery_group_receipt_get("group-002")
        assert receipt["expected_count"] == 5  # 原值不被覆盖
        assert receipt["action_id"] == "action-002"

    @pytest.mark.asyncio
    async def test_confirm_child_increments_count(self, cache_store):
        """confirm_child 递增 confirmed_count 并返回新值。"""
        store = cache_store
        await store.delivery_group_receipt_create(
            "group-003", expected_count=3,
            source_ids=[1], target_ids=[2, 3, 4],
            action_id="action-003",
        )
        c1 = await store.delivery_group_receipt_confirm_child("group-003", 1)
        assert c1 == 1
        c2 = await store.delivery_group_receipt_confirm_child("group-003", 2)
        assert c2 == 2
        receipt = await store.delivery_group_receipt_get("group-003")
        assert receipt["confirmed_count"] == 2
        assert receipt["status"] == "partial"  # 2 < 3

    @pytest.mark.asyncio
    async def test_confirm_child_transitions_to_completed(self, cache_store):
        """confirmed_count 达到 expected_count 时 status 迁移到 completed。"""
        store = cache_store
        await store.delivery_group_receipt_create(
            "group-004", expected_count=2,
            source_ids=[1], target_ids=[2, 3],
            action_id="action-004",
        )
        await store.delivery_group_receipt_confirm_child("group-004", 1)
        c2 = await store.delivery_group_receipt_confirm_child("group-004", 2)
        assert c2 == 2
        receipt = await store.delivery_group_receipt_get("group-004")
        assert receipt["status"] == "completed"

    @pytest.mark.asyncio
    async def test_confirm_child_nonexistent_returns_none(self, cache_store):
        """confirm_child 不存在的 group_id 返回 None。"""
        store = cache_store
        result = await store.delivery_group_receipt_confirm_child("nonexistent", 1)
        assert result is None

    @pytest.mark.asyncio
    async def test_list_pending_returns_only_pending_and_partial(self, cache_store):
        """list_pending 仅返回 pending 和 partial 状态的回执。"""
        store = cache_store
        # group-pending: pending
        await store.delivery_group_receipt_create(
            "group-pending", expected_count=2,
            source_ids=[1], target_ids=[2, 3],
            action_id="a-pending",
        )
        # group-partial: partial
        await store.delivery_group_receipt_create(
            "group-partial", expected_count=3,
            source_ids=[1], target_ids=[2, 3, 4],
            action_id="a-partial",
        )
        await store.delivery_group_receipt_confirm_child("group-partial", 1)
        # group-completed: completed
        await store.delivery_group_receipt_create(
            "group-completed", expected_count=1,
            source_ids=[1], target_ids=[2],
            action_id="a-completed",
        )
        await store.delivery_group_receipt_confirm_child("group-completed", 1)
        pending = await store.delivery_group_receipt_list_pending()
        group_ids = {r["group_id"] for r in pending}
        assert "group-pending" in group_ids
        assert "group-partial" in group_ids
        assert "group-completed" not in group_ids

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, cache_store):
        """查询不存在的 group_id 返回 None。"""
        store = cache_store
        result = await store.delivery_group_receipt_get("nonexistent")
        assert result is None


# ════════════════════════════════════════════════════════════════
# 5. callback_nonces 原子消费(P1-a)
# ════════════════════════════════════════════════════════════════

class TestCallbackNonces:
    """R47 P1-a: callback_nonces 原子消费测试。"""

    @pytest.mark.asyncio
    async def test_create_and_exists(self, cache_store):
        """创建 nonce 后 exists 返回 True。"""
        store = cache_store
        ok = await store.callback_nonce_create(
            "nonce-001", principal_id=1001,
            action="approval_callback",
            expires_at="2026-12-31T23:59:59",
        )
        assert ok is True
        assert await store.callback_nonce_exists("nonce-001") is True

    @pytest.mark.asyncio
    async def test_exists_nonexistent_returns_false(self, cache_store):
        """不存在的 nonce exists 返回 False。"""
        store = cache_store
        assert await store.callback_nonce_exists("nonexistent") is False

    @pytest.mark.asyncio
    async def test_create_idempotent(self, cache_store):
        """重复创建相同 nonce 应幂等(不报错)。"""
        store = cache_store
        await store.callback_nonce_create(
            "nonce-002", principal_id=1002,
            action="approval_callback",
            expires_at="2026-12-31T23:59:59",
        )
        # 再次创建应成功(INSERT OR IGNORE)
        ok = await store.callback_nonce_create(
            "nonce-002", principal_id=1002,
            action="approval_callback",
            expires_at="2026-12-31T23:59:59",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_consume_first_time_succeeds(self, cache_store):
        """首次消费 nonce 返回 True。"""
        store = cache_store
        await store.callback_nonce_create(
            "nonce-003", principal_id=1003,
            action="approval_callback",
            expires_at="2026-12-31T23:59:59",
        )
        result = await store.callback_nonce_consume("nonce-003")
        assert result is True

    @pytest.mark.asyncio
    async def test_consume_second_time_fails(self, cache_store):
        """重复消费同一 nonce 返回 False(防重放)。"""
        store = cache_store
        await store.callback_nonce_create(
            "nonce-004", principal_id=1004,
            action="approval_callback",
            expires_at="2026-12-31T23:59:59",
        )
        # 首次消费成功
        assert await store.callback_nonce_consume("nonce-004") is True
        # 第二次消费应失败(已消费)
        assert await store.callback_nonce_consume("nonce-004") is False

    @pytest.mark.asyncio
    async def test_consume_nonexistent_returns_false(self, cache_store):
        """消费不存在的 nonce 返回 False。"""
        store = cache_store
        result = await store.callback_nonce_consume("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_concurrent_consume_only_one_succeeds(self, cache_store):
        """并发消费同一 nonce,仅一个成功(原子性)。"""
        store = cache_store
        await store.callback_nonce_create(
            "nonce-concurrent", principal_id=1005,
            action="approval_callback",
            expires_at="2026-12-31T23:59:59",
        )
        # 并发消费 5 次
        results = await asyncio.gather(*[
            store.callback_nonce_consume("nonce-concurrent")
            for _ in range(5)
        ])
        # 仅一个 True,其余 False
        assert results.count(True) == 1, (
            f"并发消费仅一个应成功,实际 {results.count(True)} 个成功"
        )
        assert results.count(False) == 4


# ════════════════════════════════════════════════════════════════
# 6. mfa_failures 新 schema(P1-b)
# ════════════════════════════════════════════════════════════════

class TestMfaFailuresNewSchema:
    """R47 P1-b: mfa_failures 新 schema(id AUTOINCREMENT + failed_at_ms)。"""

    @pytest.mark.asyncio
    async def test_mfa_failures_table_has_new_columns(self, cache_store):
        """mfa_failures 表应有 id/failed_at_ms 列,无 failed_at 列。"""
        store = cache_store
        cursor = await store._db.execute("PRAGMA table_info(mfa_failures)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "id" in cols, "新 schema 应有 id 列"
        assert "failed_at_ms" in cols, "新 schema 应有 failed_at_ms 列"
        assert "principal_id" in cols
        assert "created_at" in cols
        # 旧 schema 的 failed_at 列不应存在(除非从旧表迁移)
        # 新建数据库不应有 failed_at 列
        assert "failed_at" not in cols, (
            "新 schema 不应有 failed_at 列(应为 failed_at_ms)"
        )

    @pytest.mark.asyncio
    async def test_mfa_failures_insert_uses_milliseconds(self, cache_store):
        """插入 mfa_failures 使用毫秒整数 failed_at_ms。"""
        store = cache_store
        import datetime as _dt
        _now_ms = int(time.time() * 1000)
        await store._db.execute(
            "INSERT INTO mfa_failures (principal_id, failed_at_ms, created_at) "
            "VALUES (?, ?, ?)",
            (2001, _now_ms, _dt.datetime.now().isoformat()),
        )
        await store._db.commit()
        cursor = await store._db.execute(
            "SELECT principal_id, failed_at_ms FROM mfa_failures "
            "WHERE principal_id = 2001",
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 2001
        assert row[1] == _now_ms

    @pytest.mark.asyncio
    async def test_mfa_failures_no_collision_on_same_millisecond(
        self, cache_store,
    ):
        """同毫秒多次插入不碰撞(新 schema 用 AUTOINCREMENT 主键)。"""
        store = cache_store
        import datetime as _dt
        _now_ms = int(time.time() * 1000)
        _now_iso = _dt.datetime.now().isoformat()
        # 同毫秒插入 3 条(旧 schema 会碰撞丢失)
        for _ in range(3):
            await store._db.execute(
                "INSERT INTO mfa_failures (principal_id, failed_at_ms, created_at) "
                "VALUES (?, ?, ?)",
                (3001, _now_ms, _now_iso),
            )
        await store._db.commit()
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_failures "
            "WHERE principal_id = 3001 AND failed_at_ms = ?",
            (_now_ms,),
        )
        row = await cursor.fetchone()
        assert row[0] == 3, (
            f"同毫秒 3 次插入应全部保留(新 schema 无碰撞),实际 {row[0]}"
        )

    @pytest.mark.asyncio
    async def test_mfa_failures_index_exists(self, cache_store):
        """idx_mfa_failures_principal_time 索引应存在。"""
        store = cache_store
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_mfa_failures_principal_time'"
        )
        row = await cursor.fetchone()
        assert row is not None, "idx_mfa_failures_principal_time 索引应存在"

    @pytest.mark.asyncio
    async def test_mfa_record_failure_and_is_locked_integration(
        self, cache_store,
    ):
        """集成测试: _record_mfa_failure + _is_locked 使用新 schema。"""
        from admin import mfa as _mfa_mod
        _mfa_mod.reset_mfa_state_for_testing()
        try:
            principal_id = 4001
            # 记录 4 次失败(未锁定,阈值 5)
            for _ in range(4):
                ok = await _mfa_mod._record_mfa_failure(principal_id)
                assert ok is True
            # 未锁定
            assert await _mfa_mod._is_locked(principal_id) is False
            # 第 5 次失败 → 锁定
            await _mfa_mod._record_mfa_failure(principal_id)
            assert await _mfa_mod._is_locked(principal_id) is True
            # 清除
            assert await _mfa_mod._clear_mfa_failures(principal_id) is True
            # 清除后未锁定
            assert await _mfa_mod._is_locked(principal_id) is False
        finally:
            _mfa_mod.reset_mfa_state_for_testing()
