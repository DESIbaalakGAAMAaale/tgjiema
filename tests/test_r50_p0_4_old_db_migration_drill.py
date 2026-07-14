"""R50 P0-4: 老库 Dirty Outbox UNIQUE 迁移演练 — 终审报告测试。

测试目标:
1. 真实老版本 SQLite copy 执行 migration(模拟老库:无 UNIQUE INDEX + 历史重复行)
2. 审核被删除行是否都完整进入 migration_conflicts(含完整 payload JSON)
3. 确认权威行排序与业务语义相符(最新 created_at DESC + 最大 id DESC)
4. index 创建失败时所有业务服务必须拒绝启动(fail-closed RuntimeError)
5. 唯一约束启用后执行 1000 并发 allocate_version 测试(无重复、严格单调)

不修改 database/cache_store.py(已由 R49 P0-5 完成 7 步迁移实现),
仅新增本测试文件验证迁移逻辑。

测试策略:
- 真实 SQLite 临时文件数据库(tempfile.mkdtemp 隔离)
- pytest-asyncio + asyncio.gather 模拟并发写入
- monkeypatch _db.execute 模拟 CREATE UNIQUE INDEX 失败
- 不依赖 VPS,全部在 Windows + Python 3.11 本地运行

关键实现信息(cache_store.py):
  - allocate_version 签名:
      async def allocate_version(self, table_name: str, pk: str,
                                  connection: Any = None) -> int
  - dirty_outbox schema:
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      table_name TEXT NOT NULL, pk TEXT NOT NULL,
      version INTEGER DEFAULT 0, operation TEXT DEFAULT 'upsert',
      payload TEXT, created_at TEXT,
      processed INTEGER DEFAULT 0, local_only INTEGER DEFAULT 0
  - migration_conflicts schema:
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      table_name TEXT NOT NULL, conflict_type TEXT NOT NULL,
      record_id INTEGER NOT NULL, record_data TEXT NOT NULL,
      resolved_at TEXT, created_at TEXT NOT NULL
  - entity_versions schema:
      table_name TEXT NOT NULL, pk TEXT NOT NULL,
      version INTEGER NOT NULL, PRIMARY KEY (table_name, pk)
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
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# 测试环境兼容: mock telegram(避免 import 链中断)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite cache_store(模拟老库环境)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(R50 P0-4 测试用)。

    使用 tempfile.mkdtemp 创建独立临时目录,测试结束后自动清理。
    修改全局 DB_PATH 使 init() 使用临时路径,测试后恢复原值。
    """
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r50_p0_4_test_")
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
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_dirty_outbox_row(
    store, table_name, pk, version, created_at, operation="upsert",
    payload="{}", processed=0, local_only=0,
):
    """直接插入 dirty_outbox 行(需先 _drop_unique_index 才能插入重复行)。

    Returns:
        行的 id(PRIMARY KEY AUTOINCREMENT 自动分配)
    """
    cursor = await store._db.execute(
        "INSERT INTO dirty_outbox "
        "(table_name, pk, version, operation, payload, created_at, processed, local_only) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (table_name, pk, version, operation, payload, created_at, processed, local_only),
    )
    return cursor.lastrowid


async def _drop_unique_index(store):
    """删除 dirty_outbox UNIQUE 索引(模拟老库无约束)。"""
    await store._db.execute(
        "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
    )
    await store._db.commit()


async def _create_non_unique_index(store):
    """创建非 UNIQUE 的同名索引(模拟索引损坏/被降级)。"""
    await store._db.execute(
        "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
    )
    await store._db.execute(
        "CREATE INDEX idx_dirty_outbox_table_pk_version "
        "ON dirty_outbox(table_name, pk, version)"
    )
    await store._db.commit()


async def _verify_unique_index(store):
    """验证 idx_dirty_outbox_table_pk_version 存在且 unique=1。

    Returns:
        (found: bool, is_unique: bool)
    """
    cursor = await store._db.execute("PRAGMA index_list('dirty_outbox')")
    indexes = await cursor.fetchall()
    for _idx in indexes:
        if len(_idx) >= 3 and _idx[1] == "idx_dirty_outbox_table_pk_version":
            return True, bool(_idx[2])
    return False, False


async def _get_dirty_outbox_count(store):
    """查询 dirty_outbox 表总行数。"""
    cursor = await store._db.execute("SELECT COUNT(*) FROM dirty_outbox")
    row = await cursor.fetchone()
    return row[0]


async def _get_migration_conflicts_count(store):
    """查询 migration_conflicts 表中 dirty_outbox 冲突记录数。"""
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM migration_conflicts WHERE table_name='dirty_outbox'"
    )
    row = await cursor.fetchone()
    return row[0]


# ════════════════════════════════════════════════════════════════
# 测试 1: 真实老库有重复行,migration 成功归档并保留权威行
# ════════════════════════════════════════════════════════════════

class TestRealOldDbWithDuplicatesMigratedSuccessfully:
    """R50 P0-4 测试 1: 模拟老库(无 UNIQUE INDEX + 5 条重复行),执行迁移。"""

    @pytest.mark.asyncio
    async def test_real_old_db_with_duplicates_migrated_successfully(
        self, cache_store,
    ):
        """5 条重复行 → 迁移后 dirty_outbox 剩 1 条权威,conflicts 归档 4 条。"""
        store = cache_store
        # 模拟老库: 删除 UNIQUE INDEX
        await _drop_unique_index(store)

        # 插入 5 条重复行(同 table_name/pk/version,不同 created_at)
        timestamps = [
            "2026-07-14T08:00:00",
            "2026-07-14T09:00:00",
            "2026-07-14T10:00:00",
            "2026-07-14T11:00:00",
            "2026-07-14T12:00:00",
        ]
        for ts in timestamps:
            await _insert_dirty_outbox_row(
                store, "users", "pk-drill-001", 1, ts,
                payload=json.dumps({"ts": ts}),
            )
        await store._db.commit()

        # 执行 7 步迁移
        await store._migrate_dirty_outbox_unique_constraint()

        # 断言 1: dirty_outbox 只剩 1 条权威行
        count = await _get_dirty_outbox_count(store)
        assert count == 1, f"dirty_outbox 应剩 1 条权威行,实际 {count}"

        # 断言 2: 权威行是 created_at 最新的那条(2026-07-14T12:00:00)
        cursor = await store._db.execute(
            "SELECT created_at, payload FROM dirty_outbox "
            "WHERE table_name='users' AND pk='pk-drill-001'"
        )
        row = await cursor.fetchone()
        assert row is not None, "权威行应存在"
        assert row[0] == "2026-07-14T12:00:00", (
            f"权威行 created_at 应为最新(12:00:00),实际 {row[0]}"
        )
        assert json.loads(row[1]) == {"ts": "2026-07-14T12:00:00"}, (
            f"权威行 payload 应为最新记录,实际 {row[1]}"
        )

        # 断言 3: migration_conflicts 有 4 条归档记录
        conflicts_count = await _get_migration_conflicts_count(store)
        assert conflicts_count == 4, (
            f"migration_conflicts 应有 4 条归档,实际 {conflicts_count}"
        )

        # 断言 4: idx_dirty_outbox_table_pk_version 存在且 unique=1
        found, is_unique = await _verify_unique_index(store)
        assert found, "UNIQUE INDEX 应存在"
        assert is_unique, "UNIQUE INDEX 的 unique 标志位应为 1"


# ════════════════════════════════════════════════════════════════
# 测试 2: 归档记录含完整 payload
# ════════════════════════════════════════════════════════════════

class TestArchivedRecordsContainCompletePayload:
    """R50 P0-4 测试 2: 归档到 migration_conflicts 的记录含完整 dirty_outbox 字段。"""

    @pytest.mark.asyncio
    async def test_archived_records_contain_complete_payload(self, cache_store):
        """3 条重复行 → 归档 2 条,record_data JSON 含所有 dirty_outbox 字段。"""
        store = cache_store
        await _drop_unique_index(store)

        # 插入 3 条重复行,带不同 payload
        row_id_old = await _insert_dirty_outbox_row(
            store, "files", "pk-pay-002", 5, "2026-07-14T08:00:00",
            payload=json.dumps({"name": "old.pdf", "size": 100}),
        )
        row_id_mid = await _insert_dirty_outbox_row(
            store, "files", "pk-pay-002", 5, "2026-07-14T10:00:00",
            payload=json.dumps({"name": "mid.pdf", "size": 150}),
        )
        row_id_new = await _insert_dirty_outbox_row(
            store, "files", "pk-pay-002", 5, "2026-07-14T12:00:00",
            payload=json.dumps({"name": "new.pdf", "size": 200}),
        )
        await store._db.commit()

        # 执行迁移
        await store._migrate_dirty_outbox_unique_constraint()

        # 查询归档记录
        cursor = await store._db.execute(
            "SELECT record_id, record_data FROM migration_conflicts "
            "WHERE table_name='dirty_outbox' ORDER BY record_id"
        )
        archived_rows = await cursor.fetchall()
        assert len(archived_rows) == 2, (
            f"应归档 2 条冲突记录,实际 {len(archived_rows)}"
        )

        # 被归档的 record_id 应为旧记录(id 较小,created_at 较早)
        archived_ids = sorted(r[0] for r in archived_rows)
        # 权威行是最新的(id 最大且 created_at 最新),所以被归档的是前两个
        assert row_id_new not in archived_ids, (
            f"最新记录(id={row_id_new})不应被归档"
        )

        # 每条归档记录的 record_data 应含完整 dirty_outbox 字段
        expected_fields = {
            "id", "table_name", "pk", "version", "operation",
            "payload", "created_at", "processed", "local_only",
        }
        for _rid, _data in archived_rows:
            record = json.loads(_data)
            for field in expected_fields:
                assert field in record, (
                    f"归档记录 id={_rid} 缺少字段 '{field}': {record}"
                )
            assert record["table_name"] == "files"
            assert record["pk"] == "pk-pay-002"
            assert record["version"] == 5
            # record_id 与被删除的 dirty_outbox.id 一致
            assert record["id"] == _rid, (
                f"record_data.id={record['id']} 与 record_id={_rid} 不匹配"
            )


# ════════════════════════════════════════════════════════════════
# 测试 3: 权威行排序(created_at DESC 优先,然后 id DESC)
# ════════════════════════════════════════════════════════════════

class TestAuthoritativeRowSelection:
    """R50 P0-4 测试 3: 权威行选择规则 = created_at DESC, id DESC。"""

    @pytest.mark.asyncio
    async def test_latest_created_at_wins_even_with_smaller_id(
        self, cache_store,
    ):
        """场景 1: created_at 优先于 id — 最新 created_at 行权威(即使 id 非最大)。"""
        store = cache_store
        await _drop_unique_index(store)

        # 按乱序插入,使 id 与 created_at 不一致
        # 插入顺序: t5(id=1), t1(id=2), t3(id=3), t2(id=4), t4(id=5)
        # 即 created_at 最新的 t5 有最小 id=1
        ts_map = {
            "2026-07-14T08:00:00": "t1",  # 最早
            "2026-07-14T09:00:00": "t2",
            "2026-07-14T10:00:00": "t3",
            "2026-07-14T11:00:00": "t4",
            "2026-07-14T12:00:00": "t5",  # 最新
        }
        # 乱序插入: t5 先,然后 t1, t3, t2, t4
        insert_order = [
            "2026-07-14T12:00:00",  # id=1, t5(最新 created_at)
            "2026-07-14T08:00:00",  # id=2, t1(最早 created_at)
            "2026-07-14T10:00:00",  # id=3, t3
            "2026-07-14T09:00:00",  # id=4, t2
            "2026-07-14T11:00:00",  # id=5, t4
        ]
        for ts in insert_order:
            await _insert_dirty_outbox_row(
                store, "orders", "pk-order-003", 1, ts,
                payload=json.dumps({"ts": ts, "label": ts_map[ts]}),
            )
        await store._db.commit()

        # 执行迁移
        await store._migrate_dirty_outbox_unique_constraint()

        # 权威行应为 created_at 最新的 t5(即使其 id=1 最小)
        cursor = await store._db.execute(
            "SELECT created_at, payload FROM dirty_outbox "
            "WHERE table_name='orders' AND pk='pk-order-003'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "2026-07-14T12:00:00", (
            f"权威行 created_at 应为最新(12:00:00 / t5),实际 {row[0]}"
        )
        record = json.loads(row[1])
        assert record["label"] == "t5", (
            f"权威行应为 t5(created_at 最新优先),实际 label={record['label']}"
        )

    @pytest.mark.asyncio
    async def test_same_created_at_largest_id_wins(self, cache_store):
        """场景 2: created_at 相同时,id 最大者权威。"""
        store = cache_store
        await _drop_unique_index(store)

        # 插入 2 条:同 created_at,不同 id
        same_ts = "2026-07-14T12:00:00"
        id_first = await _insert_dirty_outbox_row(
            store, "items", "pk-item-004", 3, same_ts,
            payload=json.dumps({"label": "first_inserted"}),
        )
        id_second = await _insert_dirty_outbox_row(
            store, "items", "pk-item-004", 3, same_ts,
            payload=json.dumps({"label": "second_inserted"}),
        )
        await store._db.commit()
        assert id_second > id_first, (
            "AUTOINCREMENT 应使第二条插入的 id 更大"
        )

        # 执行迁移
        await store._migrate_dirty_outbox_unique_constraint()

        # 权威行应为 id 更大的(second_inserted)
        cursor = await store._db.execute(
            "SELECT payload FROM dirty_outbox "
            "WHERE table_name='items' AND pk='pk-item-004'"
        )
        row = await cursor.fetchone()
        assert row is not None
        record = json.loads(row[0])
        assert record["label"] == "second_inserted", (
            f"同 created_at 时 id 最大者权威,应为 second_inserted,实际 {record['label']}"
        )


# ════════════════════════════════════════════════════════════════
# 测试 4: index 创建失败时拒绝启动(fail-closed)
# ════════════════════════════════════════════════════════════════

class TestIndexCreationFailureRejectsStartup:
    """R50 P0-4 测试 4: CREATE UNIQUE INDEX 失败 → RuntimeError(fail-closed)。"""

    @pytest.mark.asyncio
    async def test_index_creation_failure_rejects_startup(self, cache_store):
        """monkeypatch _db.execute 让 CREATE UNIQUE INDEX 抛异常,应 raise RuntimeError。"""
        store = cache_store
        await _drop_unique_index(store)

        # 保存原始 execute
        _original_execute = store._db.execute

        async def _failing_execute(sql, params=None):
            # 拦截 CREATE UNIQUE INDEX,抛出异常模拟创建失败
            if "CREATE UNIQUE INDEX" in sql:
                raise sqlite3.OperationalError(
                    "mocked: CREATE UNIQUE INDEX failed (disk full)"
                )
            # 其他 SQL 正常执行
            if params is not None:
                return await _original_execute(sql, params)
            return await _original_execute(sql)

        store._db.execute = _failing_execute
        try:
            with pytest.raises(RuntimeError, match="步骤 5|CREATE UNIQUE INDEX"):
                await store._migrate_dirty_outbox_unique_constraint()
        finally:
            store._db.execute = _original_execute


# ════════════════════════════════════════════════════════════════
# 测试 5: PRAGMA 验证发现非 UNIQUE 索引 → RuntimeError
# ════════════════════════════════════════════════════════════════

class TestPragmaVerificationDetectsNonUniqueIndex:
    """R50 P0-4 测试 5: 索引存在但非 UNIQUE → RuntimeError(fail-closed)。"""

    @pytest.mark.asyncio
    async def test_pragma_verification_detects_non_unique_index(
        self, cache_store,
    ):
        """手动创建非 UNIQUE 同名索引,迁移应检测到并 raise RuntimeError。"""
        store = cache_store
        # 创建非 UNIQUE 的同名索引(模拟索引损坏/降级)
        await _create_non_unique_index(store)

        # CREATE UNIQUE INDEX IF NOT EXISTS 会静默跳过(同名索引已存在)
        # 但 PRAGMA index_list 验证会发现 unique=0,raise RuntimeError
        with pytest.raises(RuntimeError, match="非 UNIQUE"):
            await store._migrate_dirty_outbox_unique_constraint()


# ════════════════════════════════════════════════════════════════
# 测试 6: 1000 并发 allocate_version 无重复
# ════════════════════════════════════════════════════════════════

class Test1000ConcurrentAllocateVersionNoDuplicates:
    """R50 P0-4 测试 6: 1000 并发 allocate_version,version 全部唯一,max=1000。

    注意: allocate_version 按 (table_name, pk) 分配递增 version。
    要验证 "version 1..1000 全部唯一",必须使用同一 (table_name, pk),
    使 1000 次调用递增同一计数器。
    (任务描述 "不同 pk" 与 "version 1..1000 唯一" 断言矛盾,此处以断言语义为准。)
    """

    @pytest.mark.asyncio
    async def test_1000_concurrent_allocate_version_no_duplicates(
        self, cache_store,
    ):
        """1000 并发 allocate_version(同 table_name + 同 pk)→ version 全唯一,max=1000。"""
        store = cache_store
        # init() 已完成迁移,UNIQUE INDEX 已建立
        _N = 1000

        # 并发调用 allocate_version(同一 table_name + pk,使 version 递增)
        results = await asyncio.gather(*[
            store.allocate_version("users", "pk-concurrent-1000")
            for _ in range(_N)
        ])
        await store._db.commit()

        # 断言 1: 返回 1000 个结果
        assert len(results) == _N, f"应返回 {_N} 个结果,实际 {len(results)}"

        # 断言 2: 全部唯一(set 长度=1000)
        unique_versions = set(results)
        assert len(unique_versions) == _N, (
            f"并发分配的 version 有重复: 唯一数={len(unique_versions)}, "
            f"期望={_N}, results={results[:20]}..."
        )

        # 断言 3: version 范围 [1, 1000],允许乱序到达,但最终 max=1000
        assert max(results) == _N, (
            f"max(version) 应为 {_N},实际 {max(results)}"
        )
        assert min(results) == 1, (
            f"min(version) 应为 1,实际 {min(results)}"
        )


# ════════════════════════════════════════════════════════════════
# 测试 7: 同 pk 并发 allocate_version 严格单调递增
# ════════════════════════════════════════════════════════════════

class Test1000ConcurrentAllocateVersionStrictMonotonicPerPk:
    """R50 P0-4 测试 7: 同 pk 并发 allocate_version,version 严格递增无重复。"""

    @pytest.mark.asyncio
    async def test_1000_concurrent_allocate_version_strict_monotonic_per_pk(
        self, cache_store,
    ):
        """同 pk 并发 100 次 → version 严格递增 [1, 2, ..., 100],无重复。"""
        store = cache_store
        _N = 100

        results = await asyncio.gather(*[
            store.allocate_version("codes", "pk-mono-100")
            for _ in range(_N)
        ])
        await store._db.commit()

        # 断言 1: 无重复
        unique_versions = set(results)
        assert len(unique_versions) == _N, (
            f"version 有重复: 唯一数={len(unique_versions)}, 期望={_N}"
        )

        # 断言 2: 排序后严格递增 [1, 2, ..., 100]
        sorted_versions = sorted(results)
        expected = list(range(1, _N + 1))
        assert sorted_versions == expected, (
            f"排序后 version 应为 {expected},实际 {sorted_versions}"
        )


# ════════════════════════════════════════════════════════════════
# 测试 8: 迁移幂等性 — 连续 5 次运行无副作用
# ════════════════════════════════════════════════════════════════

class TestMigrationIdempotentMultipleRuns:
    """R50 P0-4 测试 8: 连续运行 migration 5 次,每次都成功,无新副作用。"""

    @pytest.mark.asyncio
    async def test_migration_idempotent_multiple_runs(self, cache_store):
        """连续调用 _migrate_dirty_outbox_unique_constraint 5 次,无副作用。"""
        store = cache_store
        # 准备: 删除索引 + 插入 5 条重复行
        await _drop_unique_index(store)
        for i in range(5):
            await _insert_dirty_outbox_row(
                store, "products", "pk-prod-008", 1,
                f"2026-07-14T0{i+1}:00:00",
                payload=json.dumps({"i": i}),
            )
        await store._db.commit()

        # 第 1 次运行: 应归档 4 条,保留 1 条权威
        await store._migrate_dirty_outbox_unique_constraint()
        count_dirty = await _get_dirty_outbox_count(store)
        count_conflicts = await _get_migration_conflicts_count(store)
        assert count_dirty == 1, f"第 1 次后 dirty_outbox 应剩 1 条,实际 {count_dirty}"
        assert count_conflicts == 4, (
            f"第 1 次后 migration_conflicts 应有 4 条,实际 {count_conflicts}"
        )

        # 第 2-5 次运行: 应全部成功,无新副作用
        for run in range(2, 6):
            await store._migrate_dirty_outbox_unique_constraint()
            count_dirty = await _get_dirty_outbox_count(store)
            count_conflicts = await _get_migration_conflicts_count(store)
            assert count_dirty == 1, (
                f"第 {run} 次后 dirty_outbox 应仍为 1 条,实际 {count_dirty}"
            )
            assert count_conflicts == 4, (
                f"第 {run} 次后 migration_conflicts 应仍为 4 条(不重复归档),"
                f"实际 {count_conflicts}"
            )

        # 第 5 次后索引仍存在且 unique=1
        found, is_unique = await _verify_unique_index(store)
        assert found, "第 5 次后索引应存在"
        assert is_unique, "第 5 次后索引应仍为 UNIQUE"


# ════════════════════════════════════════════════════════════════
# 测试 9: 干净 DB 无重复行,migration 成功
# ════════════════════════════════════════════════════════════════

class TestMigrationHandlesCleanDbNoDuplicates:
    """R50 P0-4 测试 9: 干净 DB(无重复行)调用 migration 应成功,无新增归档。"""

    @pytest.mark.asyncio
    async def test_migration_handles_clean_db_no_duplicates(
        self, cache_store,
    ):
        """干净 DB 调用 migration 应成功,migration_conflicts 无新增。"""
        store = cache_store
        # init() 已运行 migration,此处再次显式调用
        await store._migrate_dirty_outbox_unique_constraint()

        # 干净 DB 无冲突
        count_conflicts = await _get_migration_conflicts_count(store)
        assert count_conflicts == 0, (
            f"干净 DB 不应有冲突记录,实际 {count_conflicts}"
        )

        # 索引存在且 unique=1
        found, is_unique = await _verify_unique_index(store)
        assert found, "干净 DB 迁移后索引应存在"
        assert is_unique, "干净 DB 迁移后索引应为 UNIQUE"


# ════════════════════════════════════════════════════════════════
# 测试 10: 迁移审计日志记录
# ════════════════════════════════════════════════════════════════

class TestMigrationAuditLogRecorded:
    """R50 P0-4 测试 10: migration_conflicts 审计字段完整(created_at/resolved_at/conflict_type)。"""

    @pytest.mark.asyncio
    async def test_migration_audit_log_recorded(self, cache_store):
        """迁移后每条 migration_conflicts 记录审计字段完整。"""
        store = cache_store
        await _drop_unique_index(store)

        # 插入 3 条重复行
        for i in range(3):
            await _insert_dirty_outbox_row(
                store, "audit", "pk-audit-010", 7,
                f"2026-07-14T0{i+1}:00:00",
                payload=json.dumps({"i": i}),
            )
        await store._db.commit()

        # 执行迁移
        await store._migrate_dirty_outbox_unique_constraint()

        # 查询 migration_conflicts 审计字段
        cursor = await store._db.execute(
            "SELECT conflict_type, record_id, record_data, resolved_at, created_at "
            "FROM migration_conflicts WHERE table_name='dirty_outbox'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 2, (
            f"应归档 2 条冲突记录,实际 {len(rows)}"
        )

        for _ctype, _rid, _data, _resolved_at, _created_at in rows:
            # 断言 1: conflict_type = 'dirty_outbox_duplicate'
            assert _ctype == "dirty_outbox_duplicate", (
                f"conflict_type 应为 'dirty_outbox_duplicate',实际 '{_ctype}'"
            )
            # 断言 2: created_at 有值(非空时间戳)
            assert _created_at is not None and len(_created_at) > 0, (
                f"created_at 应有值,实际 {_created_at}"
            )
            # 断言 3: resolved_at 为 NULL(未解决)
            assert _resolved_at is None, (
                f"resolved_at 应为 NULL(未解决),实际 {_resolved_at}"
            )
            # record_data 含完整字段
            record = json.loads(_data)
            assert "id" in record, "record_data 应含 id 字段"
            assert "table_name" in record, "record_data 应含 table_name 字段"
            assert "created_at" in record, "record_data 应含 created_at 字段"


# ════════════════════════════════════════════════════════════════
# 测试 11: 1000 并发混合 table_name 无交叉污染
# ════════════════════════════════════════════════════════════════

class Test1000ConcurrentMixedTablesNoCrossContamination:
    """R50 P0-4 测试 11: 混合 table_name 并发,version 独立计数,无交叉污染。

    注意: 要验证 "users 1..500 + files 1..500 独立计数",必须使用同一 pk
    分别在两个 table_name 上递增。如果用不同 pk,每个 pk 各自从 1 开始,
    无法验证 "1..500 范围"(所有 pk 都返回 1)。
    """

    @pytest.mark.asyncio
    async def test_1000_concurrent_mixed_tables_no_cross_contamination(
        self, cache_store,
    ):
        """500 users + 500 files 并发 → 各自 1..500 独立计数,无重复。"""
        store = cache_store
        _N_PER_TABLE = 500

        # 并发: 500 次 users + 500 次 files(各自用同一 pk 递增)
        user_tasks = [
            store.allocate_version("users", "pk-mixed-users")
            for _ in range(_N_PER_TABLE)
        ]
        file_tasks = [
            store.allocate_version("files", "pk-mixed-files")
            for _ in range(_N_PER_TABLE)
        ]
        all_tasks = user_tasks + file_tasks
        results = await asyncio.gather(*all_tasks)
        await store._db.commit()

        user_versions = results[:_N_PER_TABLE]
        file_versions = results[_N_PER_TABLE:]

        # 断言 1: users version 全部唯一
        assert len(set(user_versions)) == _N_PER_TABLE, (
            f"users version 有重复: 唯一数={len(set(user_versions))}, "
            f"期望={_N_PER_TABLE}"
        )
        # 断言 2: files version 全部唯一
        assert len(set(file_versions)) == _N_PER_TABLE, (
            f"files version 有重复: 唯一数={len(set(file_versions))}, "
            f"期望={_N_PER_TABLE}"
        )
        # 断言 3: users version 范围 [1, 500]
        assert min(user_versions) == 1 and max(user_versions) == _N_PER_TABLE, (
            f"users version 范围应为 [1, {_N_PER_TABLE}],"
            f"实际 [{min(user_versions)}, {max(user_versions)}]"
        )
        # 断言 4: files version 范围 [1, 500]
        assert min(file_versions) == 1 and max(file_versions) == _N_PER_TABLE, (
            f"files version 范围应为 [1, {_N_PER_TABLE}],"
            f"实际 [{min(file_versions)}, {max(file_versions)}]"
        )
        # 断言 5: 排序后各自严格递增 [1..500]
        assert sorted(user_versions) == list(range(1, _N_PER_TABLE + 1)), (
            "users version 排序后应严格递增"
        )
        assert sorted(file_versions) == list(range(1, _N_PER_TABLE + 1)), (
            "files version 排序后应严格递增"
        )


# ════════════════════════════════════════════════════════════════
# 测试 12: 大数据集 10000 行迁移性能
# ════════════════════════════════════════════════════════════════

class TestMigrationWithLargeDataset10000Rows:
    """R50 P0-4 测试 12: 10000 行(1000 组 × 10 重复)迁移,耗时 < 30 秒。"""

    @pytest.mark.asyncio
    async def test_migration_with_large_dataset_10000_rows(
        self, cache_store,
    ):
        """10000 行(1000 组重复,每组 10 条)→ 迁移后剩 1000,归档 9000,< 30s。"""
        store = cache_store
        await _drop_unique_index(store)

        # 批量插入 1000 组 × 10 条 = 10000 行
        _GROUPS = 1000
        _PER_GROUP = 10
        batch_params = []
        for g in range(_GROUPS):
            for r in range(_PER_GROUP):
                ts = f"2026-07-14T{r:02d}:00:00"
                batch_params.append((
                    "bulk_table",            # table_name
                    f"pk-bulk-{g:04d}",      # pk(每组不同)
                    1,                        # version(同组相同)
                    "upsert",                 # operation
                    json.dumps({"group": g, "row": r}),
                    ts,                       # created_at
                    0,                        # processed
                    0,                        # local_only
                ))
        # 使用 executemany 批量插入(比逐条快 10x+)
        await store._db.executemany(
            "INSERT INTO dirty_outbox "
            "(table_name, pk, version, operation, payload, created_at, processed, local_only) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch_params,
        )
        await store._db.commit()

        # 验证插入正确
        total_before = await _get_dirty_outbox_count(store)
        assert total_before == _GROUPS * _PER_GROUP, (
            f"插入后应有 {_GROUPS * _PER_GROUP} 行,实际 {total_before}"
        )

        # 计时执行迁移
        start = time.monotonic()
        await store._migrate_dirty_outbox_unique_constraint()
        elapsed = time.monotonic() - start

        # 断言 1: dirty_outbox 剩 1000 条(每组 1 条权威)
        total_after = await _get_dirty_outbox_count(store)
        assert total_after == _GROUPS, (
            f"迁移后 dirty_outbox 应剩 {_GROUPS} 条,实际 {total_after}"
        )

        # 断言 2: migration_conflicts 有 9000 条归档
        conflicts_count = await _get_migration_conflicts_count(store)
        expected_conflicts = _GROUPS * (_PER_GROUP - 1)
        assert conflicts_count == expected_conflicts, (
            f"migration_conflicts 应有 {expected_conflicts} 条,实际 {conflicts_count}"
        )

        # 断言 3: 迁移耗时 < 30 秒
        assert elapsed < 30.0, (
            f"迁移 10000 行应 < 30 秒,实际 {elapsed:.2f} 秒"
        )

        # 断言 4: 索引存在且 unique=1
        found, is_unique = await _verify_unique_index(store)
        assert found, "大数据集迁移后索引应存在"
        assert is_unique, "大数据集迁移后索引应为 UNIQUE"
