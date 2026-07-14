"""R48 P0-5: dirty_outbox UNIQUE 约束迁移 fail-fast 整改测试。

测试覆盖:
1. 重复行检测 + 归档到 migration_conflicts
2. 权威记录保留(最新 created_at 或最大 id)
3. UNIQUE INDEX 创建成功
4. PRAGMA 验证索引存在且为 UNIQUE
5. 验证失败 raise RuntimeError(索引缺失/非 UNIQUE)
6. migration_conflicts 表 DDL 幂等
7. init() fail-fast:老库存在非 UNIQUE 同名索引时拒绝启动
8. 归档方法幂等(无重复行时无副作用)

测试策略:
- 真实 SQLite 临时文件数据库(隔离生产数据)
- 通过 DROP INDEX + 手动插入重复行模拟老库脏数据
- 不依赖 mock,验证真实 SQLite 行为
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
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
    """创建临时文件数据库的 CacheStore 实例(R48 测试用)。"""
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r48_p0_5_test_")
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
    """直接插入 dirty_outbox 行(绕过 UNIQUE 约束检查需要先 DROP INDEX)。"""
    await store._db.execute(
        "INSERT INTO dirty_outbox "
        "(table_name, pk, version, operation, payload, created_at, processed, local_only) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (table_name, pk, version, operation, payload, created_at, processed, local_only),
    )


# ════════════════════════════════════════════════════════════════
# 1. 重复行检测 + 归档到 migration_conflicts
# ════════════════════════════════════════════════════════════════

class TestArchiveConflictsToMigrationConflicts:
    """R48 P0-5: 重复行检测 + 归档到 migration_conflicts。"""

    @pytest.mark.asyncio
    async def test_duplicate_rows_archived(self, cache_store):
        """重复行应被检测并归档到 migration_conflicts 表。"""
        store = cache_store
        # 删除 UNIQUE 索引以插入重复行(模拟老库脏数据)
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.commit()
        # 插入 3 条相同 (table_name, pk, version) 的重复行
        await _insert_dirty_outbox_row(
            store, "users", "user-dup-001", 1, "2026-07-14T10:00:00",
        )
        await _insert_dirty_outbox_row(
            store, "users", "user-dup-001", 1, "2026-07-14T11:00:00",
        )
        await _insert_dirty_outbox_row(
            store, "users", "user-dup-001", 1, "2026-07-14T12:00:00",
        )
        await store._db.commit()
        # 执行归档
        archived = await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        # 应归档 2 条(保留 1 条权威)
        assert archived == 2, f"应归档 2 条重复行,实际 {archived}"
        # migration_conflicts 应有 2 条记录
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM migration_conflicts "
            "WHERE table_name = 'dirty_outbox'"
        )
        row = await cursor.fetchone()
        assert row[0] == 2, f"migration_conflicts 应有 2 条,实际 {row[0]}"
        # dirty_outbox 应只剩 1 条
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM dirty_outbox "
            "WHERE table_name = 'users' AND pk = 'user-dup-001' AND version = 1"
        )
        row = await cursor.fetchone()
        assert row[0] == 1, f"dirty_outbox 应剩 1 条权威记录,实际 {row[0]}"

    @pytest.mark.asyncio
    async def test_archive_preserves_record_data(self, cache_store):
        """归档的记录应保存原始行数据(JSON 序列化)。"""
        store = cache_store
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.commit()
        # 插入重复行,带特定 payload
        await _insert_dirty_outbox_row(
            store, "files", "file-001", 5, "2026-07-14T10:00:00",
            payload=json.dumps({"name": "old.pdf"}),
        )
        await _insert_dirty_outbox_row(
            store, "files", "file-001", 5, "2026-07-14T12:00:00",
            payload=json.dumps({"name": "new.pdf"}),
        )
        await store._db.commit()
        await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        # 查询归档记录
        cursor = await store._db.execute(
            "SELECT record_id, record_data FROM migration_conflicts "
            "WHERE table_name = 'dirty_outbox' ORDER BY record_id"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1  # 归档 1 条
        _record = json.loads(rows[0][1])
        # 归档的应为旧记录(payload=old.pdf, created_at 较早)
        assert _record["payload"] == json.dumps({"name": "old.pdf"})
        assert _record["table_name"] == "files"
        assert _record["pk"] == "file-001"
        assert _record["version"] == 5

    @pytest.mark.asyncio
    async def test_archive_idempotent_no_duplicates(self, cache_store):
        """归档方法幂等:无重复行时再次调用返回 0,无副作用。"""
        store = cache_store
        # 正常 init 后无重复行,调用应返回 0
        archived = await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        assert archived == 0


# ════════════════════════════════════════════════════════════════
# 2. 权威记录保留(最新 created_at 或最大 id)
# ════════════════════════════════════════════════════════════════

class TestAuthoritativeRecordRetention:
    """R48 P0-5: 权威记录保留逻辑(最新 created_at 或最大 id)。"""

    @pytest.mark.asyncio
    async def test_retains_latest_created_at(self, cache_store):
        """保留 created_at 最新的记录为权威。"""
        store = cache_store
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.commit()
        # 三条重复行,created_at 递增,payload 各不同
        await _insert_dirty_outbox_row(
            store, "t", "pk-1", 1, "2026-07-14T08:00:00",
            payload='{"v": "oldest"}',
        )
        await _insert_dirty_outbox_row(
            store, "t", "pk-1", 1, "2026-07-14T10:00:00",
            payload='{"v": "middle"}',
        )
        await _insert_dirty_outbox_row(
            store, "t", "pk-1", 1, "2026-07-14T12:00:00",
            payload='{"v": "newest"}',
        )
        await store._db.commit()
        await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        # 查询保留的权威记录
        cursor = await store._db.execute(
            "SELECT payload, created_at FROM dirty_outbox "
            "WHERE table_name = 't' AND pk = 'pk-1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == '{"v": "newest"}', (
            f"应保留 created_at 最新的记录,实际 payload={row[0]}"
        )

    @pytest.mark.asyncio
    async def test_retains_max_id_when_created_at_equal(self, cache_store):
        """created_at 相同时,保留 id 最大(最新插入)的记录。"""
        store = cache_store
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.commit()
        # 两条重复行,created_at 相同(id 递增)
        await _insert_dirty_outbox_row(
            store, "t", "pk-2", 1, "2026-07-14T10:00:00",
            payload='{"v": "first"}',
        )
        await _insert_dirty_outbox_row(
            store, "t", "pk-2", 1, "2026-07-14T10:00:00",
            payload='{"v": "second"}',
        )
        await store._db.commit()
        await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        cursor = await store._db.execute(
            "SELECT payload FROM dirty_outbox "
            "WHERE table_name = 't' AND pk = 'pk-2'"
        )
        row = await cursor.fetchone()
        assert row[0] == '{"v": "second"}', (
            f"created_at 相同时应保留 id 最大记录,实际 payload={row[0]}"
        )

    @pytest.mark.asyncio
    async def test_multiple_groups_archived_independently(self, cache_store):
        """多组重复行应独立归档,每组各自保留权威记录。"""
        store = cache_store
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.commit()
        # 组 A: (t, pk-A, 1) × 2
        await _insert_dirty_outbox_row(store, "t", "pk-A", 1, "2026-07-14T10:00:00")
        await _insert_dirty_outbox_row(store, "t", "pk-A", 1, "2026-07-14T12:00:00")
        # 组 B: (t, pk-B, 1) × 3
        await _insert_dirty_outbox_row(store, "t", "pk-B", 1, "2026-07-14T10:00:00")
        await _insert_dirty_outbox_row(store, "t", "pk-B", 1, "2026-07-14T11:00:00")
        await _insert_dirty_outbox_row(store, "t", "pk-B", 1, "2026-07-14T12:00:00")
        await store._db.commit()
        archived = await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        # 组 A 归档 1 条 + 组 B 归档 2 条 = 3 条
        assert archived == 3, f"应归档 3 条(1+2),实际 {archived}"
        # 每组应各剩 1 条
        cursor = await store._db.execute(
            "SELECT pk, COUNT(*) FROM dirty_outbox WHERE table_name = 't' "
            "GROUP BY pk ORDER BY pk"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 2
        for _pk, _cnt in rows:
            assert _cnt == 1, f"pk={_pk} 应剩 1 条,实际 {_cnt}"


# ════════════════════════════════════════════════════════════════
# 3. UNIQUE INDEX 创建成功 + PRAGMA 验证
# ════════════════════════════════════════════════════════════════

class TestUniqueIndexCreationAndVerification:
    """R48 P0-5: UNIQUE INDEX 创建 + PRAGMA 验证。"""

    @pytest.mark.asyncio
    async def test_unique_index_exists_after_init(self, cache_store):
        """init() 完成后 UNIQUE INDEX 应存在。"""
        store = cache_store
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_dirty_outbox_table_pk_version'"
        )
        row = await cursor.fetchone()
        assert row is not None, "idx_dirty_outbox_table_pk_version 索引应存在"

    @pytest.mark.asyncio
    async def test_pragma_verify_unique_flag(self, cache_store):
        """PRAGMA index_list 应报告索引 unique=1。"""
        store = cache_store
        await store._verify_unique_constraint_or_fail(
            "dirty_outbox", "idx_dirty_outbox_table_pk_version",
            ["table_name", "pk", "version"],
        )
        # 不抛异常即通过

    @pytest.mark.asyncio
    async def test_unique_index_created_after_archive(self, cache_store):
        """归档重复行后,应能成功创建 UNIQUE INDEX。"""
        store = cache_store
        # 删除索引,插入重复行
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.commit()
        await _insert_dirty_outbox_row(store, "t", "pk", 1, "2026-07-14T10:00:00")
        await _insert_dirty_outbox_row(store, "t", "pk", 1, "2026-07-14T12:00:00")
        await store._db.commit()
        # 归档
        await store._archive_conflicts_to_migration_conflicts(
            "dirty_outbox", ["table_name", "pk", "version"]
        )
        # 创建 UNIQUE INDEX(应成功)
        await store._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_dirty_outbox_table_pk_version "
            "ON dirty_outbox(table_name, pk, version)"
        )
        await store._db.commit()
        # PRAGMA 验证
        await store._verify_unique_constraint_or_fail(
            "dirty_outbox", "idx_dirty_outbox_table_pk_version",
            ["table_name", "pk", "version"],
        )
        # 不抛异常即通过


# ════════════════════════════════════════════════════════════════
# 4. 验证失败 raise RuntimeError
# ════════════════════════════════════════════════════════════════

class TestVerifyUniqueConstraintFailFast:
    """R48 P0-5: PRAGMA 验证失败时 raise RuntimeError(fail-fast)。"""

    @pytest.mark.asyncio
    async def test_raises_when_index_missing(self, cache_store):
        """索引不存在时应 raise RuntimeError。"""
        store = cache_store
        with pytest.raises(RuntimeError, match="不存在"):
            await store._verify_unique_constraint_or_fail(
                "dirty_outbox", "nonexistent_index_name",
                ["table_name", "pk", "version"],
            )

    @pytest.mark.asyncio
    async def test_raises_when_index_not_unique(self, cache_store):
        """索引存在但非 UNIQUE 时应 raise RuntimeError。"""
        store = cache_store
        # 删除 UNIQUE 索引,创建同名非 UNIQUE 索引
        await store._db.execute(
            "DROP INDEX IF EXISTS idx_dirty_outbox_table_pk_version"
        )
        await store._db.execute(
            "CREATE INDEX idx_dirty_outbox_table_pk_version "
            "ON dirty_outbox(table_name, pk, version)"
        )
        await store._db.commit()
        with pytest.raises(RuntimeError, match="非 UNIQUE"):
            await store._verify_unique_constraint_or_fail(
                "dirty_outbox", "idx_dirty_outbox_table_pk_version",
                ["table_name", "pk", "version"],
            )

    @pytest.mark.asyncio
    async def test_raises_when_db_not_initialized(self):
        """数据库连接未初始化时应 raise RuntimeError。"""
        from database import cache_store as cs_module
        s = cs_module.CacheStore()
        with pytest.raises(RuntimeError, match="数据库连接未初始化"):
            await s._verify_unique_constraint_or_fail(
                "dirty_outbox", "idx_dirty_outbox_table_pk_version",
                ["table_name", "pk", "version"],
            )


# ════════════════════════════════════════════════════════════════
# 5. migration_conflicts 表 DDL 幂等
# ════════════════════════════════════════════════════════════════

class TestMigrationConflictsTableDDL:
    """R48 P0-5: migration_conflicts 表 DDL 幂等性。"""

    @pytest.mark.asyncio
    async def test_table_exists_after_init(self, cache_store):
        """init() 后 migration_conflicts 表应存在。"""
        store = cache_store
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='migration_conflicts'"
        )
        row = await cursor.fetchone()
        assert row is not None, "migration_conflicts 表应存在"

    @pytest.mark.asyncio
    async def test_table_columns_correct(self, cache_store):
        """migration_conflicts 表应有正确列结构。"""
        store = cache_store
        cursor = await store._db.execute("PRAGMA table_info(migration_conflicts)")
        cols = {row[1]: row[2] for row in await cursor.fetchall()}
        assert "id" in cols
        assert "table_name" in cols
        assert "conflict_type" in cols
        assert "record_id" in cols
        assert "record_data" in cols
        assert "resolved_at" in cols
        assert "created_at" in cols

    @pytest.mark.asyncio
    async def test_ddl_idempotent(self, cache_store):
        """重复执行 CREATE TABLE IF NOT EXISTS 不报错(幂等)。"""
        store = cache_store
        # 重复创建表(应无异常)
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS migration_conflicts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name    TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                record_id     INTEGER NOT NULL,
                record_data   TEXT NOT NULL,
                resolved_at   TEXT,
                created_at    TEXT NOT NULL
            )"""
        )
        await store._db.commit()
        # 表仍应存在且可用
        await store._db.execute(
            "INSERT INTO migration_conflicts "
            "(table_name, conflict_type, record_id, record_data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test_table", "test_conflict", 999, "{}", "2026-07-14T00:00:00"),
        )
        await store._db.commit()
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM migration_conflicts WHERE record_id = 999"
        )
        row = await cursor.fetchone()
        assert row[0] == 1

    @pytest.mark.asyncio
    async def test_unresolved_index_exists(self, cache_store):
        """idx_migration_conflicts_unresolved 索引应存在。"""
        store = cache_store
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_migration_conflicts_unresolved'"
        )
        row = await cursor.fetchone()
        assert row is not None, "idx_migration_conflicts_unresolved 索引应存在"


# ════════════════════════════════════════════════════════════════
# 6. init() fail-fast 集成测试
# ════════════════════════════════════════════════════════════════

class TestInitFailFastIntegration:
    """R48 P0-5: init() fail-fast 集成测试 — 老库非 UNIQUE 同名索引时拒绝启动。"""

    @pytest.mark.asyncio
    async def test_init_raises_on_non_unique_index(self):
        """老库存在非 UNIQUE 同名索引时,init() 应 raise RuntimeError。"""
        from database import cache_store as cs_module

        tmpdir = tempfile.mkdtemp(prefix="r48_failfast_test_")
        db_path = Path(tmpdir) / "test_failfast.db"
        original_path = cs_module.DB_PATH
        original_store = getattr(cs_module, "_store", None)
        cs_module.DB_PATH = db_path
        try:
            # 1. 预创建老库:dirty_outbox 表 + 非 UNIQUE 同名索引
            pre_conn = sqlite3.connect(str(db_path))
            pre_conn.execute(
                """CREATE TABLE dirty_outbox (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name  TEXT NOT NULL,
                    pk          TEXT NOT NULL,
                    version     INTEGER DEFAULT 0,
                    operation   TEXT DEFAULT 'upsert',
                    payload     TEXT,
                    created_at  TEXT,
                    processed   INTEGER DEFAULT 0,
                    local_only  INTEGER DEFAULT 0
                )"""
            )
            # 创建非 UNIQUE 索引(模拟老库错误的非唯一索引)
            pre_conn.execute(
                "CREATE INDEX idx_dirty_outbox_table_pk_version "
                "ON dirty_outbox(table_name, pk, version)"
            )
            pre_conn.commit()
            pre_conn.close()

            # 2. 调用 init() — 应 raise RuntimeError(PRAGMA 验证发现非 UNIQUE)
            s = cs_module.CacheStore()
            with pytest.raises(RuntimeError, match="非 UNIQUE"):
                await s.init()
            # 清理
            if s._db:
                await s.close()
        finally:
            cs_module.DB_PATH = original_path
            if original_store is not None:
                cs_module._store = original_store
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_init_succeeds_on_clean_db(self):
        """干净数据库(无老库脏数据)init() 应成功。"""
        from database import cache_store as cs_module

        tmpdir = tempfile.mkdtemp(prefix="r48_clean_test_")
        db_path = Path(tmpdir) / "test_clean.db"
        original_path = cs_module.DB_PATH
        original_store = getattr(cs_module, "_store", None)
        cs_module.DB_PATH = db_path
        try:
            s = cs_module.CacheStore()
            await s.init()  # 不抛异常即通过
            # 验证索引存在且 UNIQUE
            await s._verify_unique_constraint_or_fail(
                "dirty_outbox", "idx_dirty_outbox_table_pk_version",
                ["table_name", "pk", "version"],
            )
            await s.close()
        finally:
            cs_module.DB_PATH = original_path
            if original_store is not None:
                cs_module._store = original_store
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_init_archives_old_duplicates_and_succeeds(self):
        """老库有重复行但无非 UNIQUE 索引时,init() 应归档后成功。"""
        from database import cache_store as cs_module

        tmpdir = tempfile.mkdtemp(prefix="r48_archive_test_")
        db_path = Path(tmpdir) / "test_archive.db"
        original_path = cs_module.DB_PATH
        original_store = getattr(cs_module, "_store", None)
        cs_module.DB_PATH = db_path
        try:
            # 1. 预创建老库:dirty_outbox 表 + 重复行(无 UNIQUE 索引)
            pre_conn = sqlite3.connect(str(db_path))
            pre_conn.execute(
                """CREATE TABLE dirty_outbox (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name  TEXT NOT NULL,
                    pk          TEXT NOT NULL,
                    version     INTEGER DEFAULT 0,
                    operation   TEXT DEFAULT 'upsert',
                    payload     TEXT,
                    created_at  TEXT,
                    processed   INTEGER DEFAULT 0,
                    local_only  INTEGER DEFAULT 0
                )"""
            )
            # 插入重复行(相同 table_name, pk, version)
            for ts in ["2026-07-14T08:00:00", "2026-07-14T10:00:00", "2026-07-14T12:00:00"]:
                pre_conn.execute(
                    "INSERT INTO dirty_outbox "
                    "(table_name, pk, version, operation, payload, created_at, processed, local_only) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                    ("users", "user-old-001", 1, "upsert", "{}", ts),
                )
            pre_conn.commit()
            pre_conn.close()

            # 2. 调用 init() — 应归档重复行后成功创建 UNIQUE 索引
            s = cs_module.CacheStore()
            await s.init()  # 不抛异常即通过

            # 3. 验证:dirty_outbox 只剩 1 条权威记录
            cursor = await s._db.execute(
                "SELECT COUNT(*) FROM dirty_outbox "
                "WHERE table_name = 'users' AND pk = 'user-old-001' AND version = 1"
            )
            row = await cursor.fetchone()
            assert row[0] == 1, f"应剩 1 条权威记录,实际 {row[0]}"

            # 4. 验证:migration_conflicts 有 2 条归档记录
            cursor = await s._db.execute(
                "SELECT COUNT(*) FROM migration_conflicts WHERE table_name = 'dirty_outbox'"
            )
            row = await cursor.fetchone()
            assert row[0] == 2, f"应归档 2 条,实际 {row[0]}"

            # 5. 验证:权威记录的 created_at 为最新
            cursor = await s._db.execute(
                "SELECT created_at FROM dirty_outbox "
                "WHERE table_name = 'users' AND pk = 'user-old-001' AND version = 1"
            )
            row = await cursor.fetchone()
            assert row[0] == "2026-07-14T12:00:00", (
                f"权威记录应为 created_at 最新,实际 {row[0]}"
            )

            await s.close()
        finally:
            cs_module.DB_PATH = original_path
            if original_store is not None:
                cs_module._store = original_store
            shutil.rmtree(tmpdir, ignore_errors=True)
