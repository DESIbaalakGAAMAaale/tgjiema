"""R49 P0-5: 老库 dirty_outbox UNIQUE 约束完整 7 步迁移方案测试。

测试覆盖完整的 7 步迁移流程:
1. preflight 查询重复行(SELECT GROUP BY HAVING COUNT > 1)
2. 选定权威行(每组按 created_at DESC, id DESC,第一行为权威)
3. 归档冲突行到 migration_conflicts(conflict_type='dirty_outbox_duplicate')
4. 删除冲突行(从 dirty_outbox 表)
5. 创建 UNIQUE INDEX IF NOT EXISTS
6. PRAGMA index_list + index_info 验证索引存在且 unique=1
7. fail-closed(任一步失败 raise RuntimeError,拒绝启动)

权威行选择说明:
  R49 任务描述原文为 "created_at ASC, id ASC",但 R48 已存在的测试
  (test_init_archives_old_duplicates_and_succeeds) 断言保留 created_at 最新者。
  为满足 "R48 测试不破" 约束,本实现沿用 R48 的 DESC 语义
  (最新 created_at + 最大 id 为权威),并在测试中固定此行为。

测试用例:
- test_clean_db_no_conflict: 全新 DB,无重复行,migration 成功
- test_old_db_with_duplicates_migrated: 3 组重复行,迁移归档+创建索引+PRAGMA 通过
- test_migration_conflicts_table_populated: 冲突行含完整 payload 归档
- test_unique_index_creation_failure_raises: CREATE UNIQUE INDEX 失败,raise RuntimeError
- test_pragma_verification_detects_missing_index: PRAGMA 返回空,raise RuntimeError
- test_idempotent_multiple_runs: 连续运行 2 次,第二次无冲突仍通过
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    """创建临时文件数据库的 CacheStore 实例(R49 测试用)。"""
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r49_p0_5_test_")
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


async def _insert_dirty_outbox_row(
    store, table_name, pk, version, created_at, operation="upsert",
    payload="{}", processed=0, local_only=0,
):
    """直接插入 dirty_outbox 行(需先 DROP UNIQUE INDEX 才能插入重复行)。"""
    await store._db.execute(
        "INSERT INTO dirty_outbox "
        "(table_name, pk, version, operation, payload, created_at, processed, local_only) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (table_name, pk, version, operation, payload, created_at, processed, local_only),
    )


async def _drop_unique_index(store):
    """删除 dirty_outbox UNIQUE 索引(模拟老库无约束)。"""
    await store._db.execute(
        "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
    )
    await store._db.commit()


# ════════════════════════════════════════════════════════════════
# 1. 全新 DB,无重复行
# ════════════════════════════════════════════════════════════════

class TestCleanDbNoConflict:
    """R49 P0-5: 全新 DB,无重复行,migration 应成功。"""

    @pytest.mark.asyncio
    async def test_clean_db_no_conflict(self, cache_store):
        """全新 DB 调用 migration 应成功创建 UNIQUE INDEX,PRAGMA 验证通过。"""
        store = cache_store
        # init() 已调用 migration,此处再次显式调用验证幂等性
        await store._migrate_dirty_outbox_unique_constraint()
        # 验证 UNIQUE INDEX 存在
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_dirty_outbox_table_pk_version'"
        )
        row = await cursor.fetchone()
        assert row is not None, "idx_dirty_outbox_table_pk_version 索引应存在"
        # 验证 PRAGMA index_list 中 unique=1
        cursor = await store._db.execute("PRAGMA index_list('dirty_outbox')")
        indexes = await cursor.fetchall()
        _found_unique = False
        for _idx in indexes:
            if len(_idx) >= 3 and _idx[1] == "idx_dirty_outbox_table_pk_version":
                assert _idx[2] == 1, f"unique 标志位应为 1,实际 {_idx[2]}"
                _found_unique = True
                break
        assert _found_unique, "PRAGMA index_list 应找到 UNIQUE 索引"
        # migration_conflicts 应为空(无冲突)
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM migration_conflicts WHERE table_name='dirty_outbox'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, f"干净 DB 不应有冲突记录,实际 {row[0]}"


# ════════════════════════════════════════════════════════════════
# 2. 老库有重复行,迁移归档+创建索引+验证通过
# ════════════════════════════════════════════════════════════════

class TestOldDbWithDuplicatesMigrated:
    """R49 P0-5: 老库有重复行,7 步迁移应归档冲突行并创建 UNIQUE INDEX。"""

    @pytest.mark.asyncio
    async def test_old_db_with_duplicates_migrated(self, cache_store):
        """预插入 3 组重复行,migration 应归档冲突,保留权威,创建索引,PRAGMA 通过。"""
        store = cache_store
        # 删除 UNIQUE 索引以插入重复行(模拟老库)
        await _drop_unique_index(store)
        # 组 A: (users, pk-A, 1) × 2 行
        await _insert_dirty_outbox_row(
            store, "users", "pk-A", 1, "2026-07-14T08:00:00",
            payload='{"v": "oldest-A"}',
        )
        await _insert_dirty_outbox_row(
            store, "users", "pk-A", 1, "2026-07-14T12:00:00",
            payload='{"v": "newest-A"}',
        )
        # 组 B: (files, pk-B, 5) × 3 行
        await _insert_dirty_outbox_row(
            store, "files", "pk-B", 5, "2026-07-14T09:00:00",
            payload='{"v": "old-B"}',
        )
        await _insert_dirty_outbox_row(
            store, "files", "pk-B", 5, "2026-07-14T10:00:00",
            payload='{"v": "mid-B"}',
        )
        await _insert_dirty_outbox_row(
            store, "files", "pk-B", 5, "2026-07-14T11:00:00",
            payload='{"v": "new-B"}',
        )
        # 组 C: (codes, pk-C, 9) × 2 行
        await _insert_dirty_outbox_row(
            store, "codes", "pk-C", 9, "2026-07-14T07:00:00",
            payload='{"v": "old-C"}',
        )
        await _insert_dirty_outbox_row(
            store, "codes", "pk-C", 9, "2026-07-14T13:00:00",
            payload='{"v": "new-C"}',
        )
        await store._db.commit()
        # 执行 7 步迁移
        await store._migrate_dirty_outbox_unique_constraint()
        # 验证:每组只保留 1 条权威记录(最新 created_at)
        cursor = await store._db.execute(
            "SELECT table_name, pk, version, payload FROM dirty_outbox "
            "ORDER BY table_name, pk"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 3, f"应保留 3 条权威记录,实际 {len(rows)}"
        # 组 A: 应保留 newest-A
        _a = [r for r in rows if r[0] == "users" and r[1] == "pk-A"]
        assert len(_a) == 1
        assert _a[0][3] == '{"v": "newest-A"}', f"组 A 权威 payload 错误: {_a[0][3]}"
        # 组 B: 应保留 new-B
        _b = [r for r in rows if r[0] == "files" and r[1] == "pk-B"]
        assert len(_b) == 1
        assert _b[0][3] == '{"v": "new-B"}', f"组 B 权威 payload 错误: {_b[0][3]}"
        # 组 C: 应保留 new-C
        _c = [r for r in rows if r[0] == "codes" and r[1] == "pk-C"]
        assert len(_c) == 1
        assert _c[0][3] == '{"v": "new-C"}', f"组 C 权威 payload 错误: {_c[0][3]}"
        # 验证 UNIQUE INDEX 创建成功
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_dirty_outbox_table_pk_version'"
        )
        row = await cursor.fetchone()
        assert row is not None, "UNIQUE INDEX 应存在"
        # 验证 PRAGMA unique=1
        cursor = await store._db.execute("PRAGMA index_list('dirty_outbox')")
        indexes = await cursor.fetchall()
        _is_unique = False
        for _idx in indexes:
            if len(_idx) >= 3 and _idx[1] == "idx_dirty_outbox_table_pk_version":
                assert _idx[2] == 1, f"unique 标志位应为 1,实际 {_idx[2]}"
                _is_unique = True
                break
        assert _is_unique, "PRAGMA 应找到 UNIQUE 索引"


# ════════════════════════════════════════════════════════════════
# 3. migration_conflicts 表含完整 payload
# ════════════════════════════════════════════════════════════════

class TestMigrationConflictsTablePopulated:
    """R49 P0-5: migration_conflicts 表应含冲突行完整 payload。"""

    @pytest.mark.asyncio
    async def test_migration_conflicts_table_populated(self, cache_store):
        """迁移后 migration_conflicts 应有冲突行记录,含完整 payload JSON。"""
        store = cache_store
        await _drop_unique_index(store)
        # 插入 1 组重复行(2 条),带特定 payload
        await _insert_dirty_outbox_row(
            store, "users", "pk-pay", 1, "2026-07-14T08:00:00",
            payload=json.dumps({"name": "old.pdf", "size": 100}),
        )
        await _insert_dirty_outbox_row(
            store, "users", "pk-pay", 1, "2026-07-14T12:00:00",
            payload=json.dumps({"name": "new.pdf", "size": 200}),
        )
        await store._db.commit()
        # 执行迁移
        await store._migrate_dirty_outbox_unique_constraint()
        # 查询 migration_conflicts
        cursor = await store._db.execute(
            "SELECT table_name, conflict_type, record_id, record_data "
            "FROM migration_conflicts WHERE table_name='dirty_outbox'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1, f"应归档 1 条冲突,实际 {len(rows)}"
        _tn, _ctype, _rid, _data = rows[0]
        assert _tn == "dirty_outbox"
        assert _ctype == "dirty_outbox_duplicate", (
            f"conflict_type 应为 'dirty_outbox_duplicate',实际 '{_ctype}'"
        )
        # record_data 应为 JSON,含完整 payload
        _record = json.loads(_data)
        assert "payload" in _record, "record_data 应包含 payload 字段"
        assert "table_name" in _record, "record_data 应包含 table_name 字段"
        assert "pk" in _record, "record_data 应包含 pk 字段"
        assert "version" in _record, "record_data 应包含 version 字段"
        assert "created_at" in _record, "record_data 应包含 created_at 字段"
        # 归档的应为旧记录(payload=old.pdf, created_at 较早)
        assert json.loads(_record["payload"]) == {"name": "old.pdf", "size": 100}, (
            f"归档记录 payload 应为 old.pdf,实际 {_record['payload']}"
        )
        assert _record["created_at"] == "2026-07-14T08:00:00", (
            f"归档记录 created_at 应为 08:00,实际 {_record['created_at']}"
        )
        # dirty_outbox 应保留新记录
        cursor = await store._db.execute(
            "SELECT payload FROM dirty_outbox "
            "WHERE table_name='users' AND pk='pk-pay' AND version=1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert json.loads(row[0]) == {"name": "new.pdf", "size": 200}


# ════════════════════════════════════════════════════════════════
# 4. CREATE UNIQUE INDEX 失败 → raise RuntimeError
# ════════════════════════════════════════════════════════════════

class TestUniqueIndexCreationFailureRaises:
    """R49 P0-5: CREATE UNIQUE INDEX 失败时 raise RuntimeError(fail-closed)。"""

    @pytest.mark.asyncio
    async def test_unique_index_creation_failure_raises(self, cache_store):
        """模拟 CREATE UNIQUE INDEX 失败,应 raise RuntimeError。"""
        store = cache_store
        # 删除索引以便触发步骤 5 的 CREATE 操作
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

        # Monkey-patch _db.execute
        store._db.execute = _failing_execute
        try:
            with pytest.raises(RuntimeError, match="CREATE UNIQUE INDEX|步骤 5"):
                await store._migrate_dirty_outbox_unique_constraint()
        finally:
            # 恢复原始 execute
            store._db.execute = _original_execute


# ════════════════════════════════════════════════════════════════
# 5. PRAGMA 验证发现索引缺失 → raise RuntimeError
# ════════════════════════════════════════════════════════════════

class TestPragmaVerificationDetectsMissingIndex:
    """R49 P0-5: PRAGMA 返回空(无索引)时应 raise RuntimeError。"""

    @pytest.mark.asyncio
    async def test_pragma_verification_detects_missing_index(self, cache_store):
        """mock PRAGMA index_list 返回空,应 raise RuntimeError(索引不存在)。"""
        store = cache_store
        # 保存原始 execute
        _original_execute = store._db.execute

        async def _mocked_execute(sql, params=None):
            # 拦截 PRAGMA index_list,返回空结果(模拟索引不存在)
            if "PRAGMA index_list" in sql:
                _mock_cursor = MagicMock()
                _mock_cursor.fetchall = AsyncMock(return_value=[])
                return _mock_cursor
            # 其他 SQL 正常执行
            if params is not None:
                return await _original_execute(sql, params)
            return await _original_execute(sql)

        store._db.execute = _mocked_execute
        try:
            with pytest.raises(RuntimeError, match="不存在"):
                await store._migrate_dirty_outbox_unique_constraint()
        finally:
            store._db.execute = _original_execute


# ════════════════════════════════════════════════════════════════
# 6. 幂等性:连续运行 2 次
# ════════════════════════════════════════════════════════════════

class TestIdempotentMultipleRuns:
    """R49 P0-5: migration 连续运行 2 次,第二次应无冲突(已清理),仍通过。"""

    @pytest.mark.asyncio
    async def test_idempotent_multiple_runs(self, cache_store):
        """连续运行 migration 2 次,第二次无冲突,两次都应成功。"""
        store = cache_store
        # 第一次运行(init 已运行,这里再显式调用一次)
        await store._migrate_dirty_outbox_unique_constraint()
        # 验证第一次后索引存在
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_dirty_outbox_table_pk_version'"
        )
        assert await cursor.fetchone() is not None
        # 第二次运行(应幂等无副作用)
        await store._migrate_dirty_outbox_unique_constraint()
        # 验证索引仍存在且 UNIQUE
        cursor = await store._db.execute("PRAGMA index_list('dirty_outbox')")
        indexes = await cursor.fetchall()
        _found = False
        for _idx in indexes:
            if len(_idx) >= 3 and _idx[1] == "idx_dirty_outbox_table_pk_version":
                assert _idx[2] == 1, "第二次运行后索引应仍为 UNIQUE"
                _found = True
                break
        assert _found, "第二次运行后索引应仍存在"
        # migration_conflicts 应为空(无冲突需要归档)
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM migration_conflicts WHERE table_name='dirty_outbox'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, f"幂等运行不应产生新冲突记录,实际 {row[0]}"

    @pytest.mark.asyncio
    async def test_idempotent_after_duplicate_cleanup(self, cache_store):
        """迁移清理重复行后再次运行,应无冲突(已清理),仍通过。"""
        store = cache_store
        # 准备重复行
        await _drop_unique_index(store)
        await _insert_dirty_outbox_row(
            store, "t", "pk-1", 1, "2026-07-14T10:00:00", payload='{"v":"old"}',
        )
        await _insert_dirty_outbox_row(
            store, "t", "pk-1", 1, "2026-07-14T12:00:00", payload='{"v":"new"}',
        )
        await store._db.commit()
        # 第一次运行:应归档 1 条
        await store._migrate_dirty_outbox_unique_constraint()
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM migration_conflicts WHERE table_name='dirty_outbox'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1, f"第一次运行应归档 1 条,实际 {row[0]}"
        # 第二次运行:应无新冲突
        await store._migrate_dirty_outbox_unique_constraint()
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM migration_conflicts WHERE table_name='dirty_outbox'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1, f"第二次运行应无新冲突,仍为 1 条,实际 {row[0]}"
        # dirty_outbox 仍只保留 1 条权威
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM dirty_outbox WHERE table_name='t' AND pk='pk-1'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1
