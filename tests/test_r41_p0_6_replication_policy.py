"""R41 P0-6 整改测试: dirty_outbox 对软删除与产品表同步完整覆盖。

测试覆盖 5 大场景:

1. ReplicationPolicy 模块结构(枚举 / 声明表 / helper 函数 / fail-closed 默认)
2. LOCAL_ONLY 表预标记(add_dirty_outbox 检查 is_local_only 预设 processed=1)
3. CRDB tombstone handler 覆盖完整性(_DIRTY_OUTBOX_TOMBSTONE_HANDLERS + _TOMBSTONE_PK_COLUMNS)
4. jobs/cells upsert handler(_dispatch_jobs_upsert / _dispatch_cells_upsert 存在并可用)
5. dispatcher tombstone 集成(_dispatch_dirty_outbox_to_crdb 路由 tombstone 到 tombstone handler)
6. dlq_records 表结构 + cleanup_dlq(insert_dlq_record / cleanup_dlq / list_dlq_records)
7. CRDB 表缺失 handler 路由到 DLQ(不静默丢弃)

测试策略:
- 真实 SQLite cache_store(临时文件 DB)用于 cache_store 相关测试
- Mock database.session(D1Collection)用于 dispatcher / tombstone 测试
- 源码静态扫描用于 handler 覆盖完整性校验
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ════════════════════════════════════════════════════════════════
# 辅助: 临时 SQLite cache_store fixture(供 cache_store 相关测试用)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(R41 P0-6 测试用)。

    使用临时目录避免污染开发环境,测试结束后自动清理。
    同时替换全局单例 _store,使 crdb_sync_service._get_cache_store_safe()
    能返回此实例。
    """
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r41_p0_6_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = cs_module.DB_PATH
    original_store = getattr(cs_module, "_store", None)
    cs_module.DB_PATH = db_path
    try:
        s = cs_module.CacheStore()
        await s.init()
        # 替换全局单例,使 get_cache_store() / _get_cache_store_safe() 返回此实例
        cs_module._store = s
        yield s
        await s.close()
    finally:
        cs_module.DB_PATH = original_path
        if original_store is not None:
            cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 场景 1: ReplicationPolicy 模块结构测试
# ════════════════════════════════════════════════════════════════

class TestReplicationPolicyModule:
    """R41 P0-6: services/replication_policy.py 模块结构校验。"""

    def test_replication_policy_enum_has_three_values(self):
        """ReplicationPolicy 枚举应包含 CRDB / LOCAL_ONLY / ARCHIVE_ONLY 三个值。"""
        from services.replication_policy import ReplicationPolicy

        values = {p.value for p in ReplicationPolicy}
        assert values == {"crdb", "local_only", "archive_only"}, (
            f"ReplicationPolicy 枚举值应为 {{crdb, local_only, archive_only}}, "
            f"实际: {values}"
        )

    def test_replication_policy_is_str_enum(self):
        """ReplicationPolicy 应继承 str(可直接做字符串比较)。"""
        from services.replication_policy import ReplicationPolicy

        # str + Enum 子类可直接与字符串比较
        assert ReplicationPolicy.CRDB == "crdb"
        assert ReplicationPolicy.LOCAL_ONLY == "local_only"
        assert ReplicationPolicy.ARCHIVE_ONLY == "archive_only"

    def test_table_replication_policy_dict_exists(self):
        """TABLE_REPLICATION_POLICY 字典应存在且非空。"""
        from services.replication_policy import TABLE_REPLICATION_POLICY

        assert isinstance(TABLE_REPLICATION_POLICY, dict)
        assert len(TABLE_REPLICATION_POLICY) > 0, "TABLE_REPLICATION_POLICY 不应为空"

    def test_table_replication_policy_covers_crdb_tables(self):
        """TABLE_REPLICATION_POLICY 应声明所有 CRDB 同步表。"""
        from services.replication_policy import (
            TABLE_REPLICATION_POLICY,
            ReplicationPolicy,
        )

        expected_crdb = {
            "users", "file_records", "codes", "decode_logs",
            "relay_whitelist", "collector_whitelist", "spare_pool",
            "channels", "cells", "jobs",
        }
        for table in expected_crdb:
            assert table in TABLE_REPLICATION_POLICY, (
                f"CRDB 表 {table} 应在 TABLE_REPLICATION_POLICY 中声明"
            )
            assert TABLE_REPLICATION_POLICY[table] is ReplicationPolicy.CRDB, (
                f"表 {table} 策略应为 CRDB, "
                f"实际: {TABLE_REPLICATION_POLICY[table]}"
            )

    def test_table_replication_policy_covers_local_only_tables(self):
        """TABLE_REPLICATION_POLICY 应声明常见 LOCAL_ONLY 表。"""
        from services.replication_policy import (
            TABLE_REPLICATION_POLICY,
            ReplicationPolicy,
        )

        expected_local = {
            "tasks", "collections", "audit_log", "approvals",
            "rbac_roles", "sessions", "kv_store", "dirty_outbox",
            "dlq_records",
        }
        for table in expected_local:
            assert table in TABLE_REPLICATION_POLICY, (
                f"LOCAL_ONLY 表 {table} 应在 TABLE_REPLICATION_POLICY 中声明"
            )
            assert TABLE_REPLICATION_POLICY[table] is ReplicationPolicy.LOCAL_ONLY, (
                f"表 {table} 策略应为 LOCAL_ONLY, "
                f"实际: {TABLE_REPLICATION_POLICY[table]}"
            )

    def test_table_replication_policy_covers_archive_only_table(self):
        """TABLE_REPLICATION_POLICY 应声明 audit_log_archive 为 ARCHIVE_ONLY。"""
        from services.replication_policy import (
            TABLE_REPLICATION_POLICY,
            ReplicationPolicy,
        )

        assert "audit_log_archive" in TABLE_REPLICATION_POLICY, (
            "audit_log_archive 应在 TABLE_REPLICATION_POLICY 中声明为 ARCHIVE_ONLY"
        )
        assert TABLE_REPLICATION_POLICY["audit_log_archive"] is ReplicationPolicy.ARCHIVE_ONLY

    def test_get_policy_returns_correct_enum(self):
        """get_policy() 返回正确的 ReplicationPolicy 枚举。"""
        from services.replication_policy import (
            get_policy,
            ReplicationPolicy,
        )

        assert get_policy("users") is ReplicationPolicy.CRDB
        assert get_policy("tasks") is ReplicationPolicy.LOCAL_ONLY
        assert get_policy("audit_log_archive") is ReplicationPolicy.ARCHIVE_ONLY

    def test_get_policy_fail_closed_for_unknown_table(self):
        """R41 P0-6: get_policy() 对未知表名 fail-closed 返回 LOCAL_ONLY。"""
        from services.replication_policy import (
            get_policy,
            ReplicationPolicy,
        )

        # 未知表名默认 LOCAL_ONLY(fail-closed,避免误同步)
        assert get_policy("unknown_table_xyz") is ReplicationPolicy.LOCAL_ONLY
        assert get_policy("") is ReplicationPolicy.LOCAL_ONLY
        assert get_policy("tasks_typo") is ReplicationPolicy.LOCAL_ONLY

    def test_is_local_only_returns_true_for_local_tables(self):
        """is_local_only() 对 LOCAL_ONLY 表返回 True。"""
        from services.replication_policy import is_local_only

        assert is_local_only("tasks") is True
        assert is_local_only("audit_log") is True
        assert is_local_only("dirty_outbox") is True
        # 未知表也返回 True(fail-closed)
        assert is_local_only("unknown_table") is True

    def test_is_local_only_returns_false_for_crdb_tables(self):
        """is_local_only() 对 CRDB 表返回 False。"""
        from services.replication_policy import is_local_only

        assert is_local_only("users") is False
        assert is_local_only("file_records") is False
        assert is_local_only("jobs") is False
        # ARCHIVE_ONLY 表也不是 local_only
        assert is_local_only("audit_log_archive") is False

    def test_is_crdb_returns_true_for_crdb_tables(self):
        """is_crdb() 对 CRDB 同步表返回 True。"""
        from services.replication_policy import is_crdb

        assert is_crdb("users") is True
        assert is_crdb("file_records") is True
        assert is_crdb("cells") is True
        assert is_crdb("jobs") is True

    def test_is_crdb_returns_false_for_non_crdb_tables(self):
        """is_crdb() 对 LOCAL_ONLY / ARCHIVE_ONLY / 未知表返回 False。"""
        from services.replication_policy import is_crdb

        assert is_crdb("tasks") is False
        assert is_crdb("audit_log_archive") is False
        assert is_crdb("unknown_table") is False

    def test_all_local_only_tables_returns_set(self):
        """all_local_only_tables() 返回非空 set。"""
        from services.replication_policy import all_local_only_tables

        result = all_local_only_tables()
        assert isinstance(result, set)
        assert len(result) > 0
        # 验证包含已知 LOCAL_ONLY 表
        assert "tasks" in result
        assert "dirty_outbox" in result
        # 验证不包含 CRDB 表
        assert "users" not in result
        assert "file_records" not in result

    def test_all_crdb_tables_returns_set(self):
        """all_crdb_tables() 返回非空 set。"""
        from services.replication_policy import all_crdb_tables

        result = all_crdb_tables()
        assert isinstance(result, set)
        assert len(result) >= 10  # 至少 10 张 CRDB 表
        assert "users" in result
        assert "file_records" in result
        assert "cells" in result
        assert "jobs" in result
        # 验证不包含 LOCAL_ONLY 表
        assert "tasks" not in result

    def test_all_archive_only_tables_returns_set(self):
        """all_archive_only_tables() 返回包含 audit_log_archive 的 set。"""
        from services.replication_policy import all_archive_only_tables

        result = all_archive_only_tables()
        assert isinstance(result, set)
        assert "audit_log_archive" in result
        # ARCHIVE_ONLY 表不应出现在 local_only / crdb 集合中
        from services.replication_policy import (
            all_local_only_tables,
            all_crdb_tables,
        )
        assert "audit_log_archive" not in all_local_only_tables()
        assert "audit_log_archive" not in all_crdb_tables()

    def test_is_archive_only_returns_true_for_archive_table(self):
        """is_archive_only() 对 ARCHIVE_ONLY 表返回 True。"""
        from services.replication_policy import is_archive_only

        assert is_archive_only("audit_log_archive") is True
        assert is_archive_only("users") is False
        assert is_archive_only("tasks") is False

    def test_all_declared_tables_equals_dict_keys(self):
        """all_declared_tables() 等于 TABLE_REPLICATION_POLICY 的键集合。"""
        from services.replication_policy import (
            all_declared_tables,
            TABLE_REPLICATION_POLICY,
        )

        assert all_declared_tables() == set(TABLE_REPLICATION_POLICY.keys())

    def test_local_only_crdb_archive_sets_disjoint(self):
        """LOCAL_ONLY / CRDB / ARCHIVE_ONLY 三个集合互不相交。"""
        from services.replication_policy import (
            all_local_only_tables,
            all_crdb_tables,
            all_archive_only_tables,
        )

        local = all_local_only_tables()
        crdb = all_crdb_tables()
        archive = all_archive_only_tables()
        # 两两不相交
        assert local.isdisjoint(crdb), "LOCAL_ONLY 与 CRDB 集合不应相交"
        assert local.isdisjoint(archive), "LOCAL_ONLY 与 ARCHIVE_ONLY 集合不应相交"
        assert crdb.isdisjoint(archive), "CRDB 与 ARCHIVE_ONLY 集合不应相交"


# ════════════════════════════════════════════════════════════════
# 场景 2: LOCAL_ONLY 表预标记(add_dirty_outbox 检查 is_local_only)
# ════════════════════════════════════════════════════════════════

class TestLocalOnlyPreMarked:
    """R41 P0-6: cache_store.add_dirty_outbox 对 LOCAL_ONLY 表预标记。"""

    @pytest.mark.asyncio
    async def test_local_only_table_preset_processed_and_local_only(self, cache_store):
        """LOCAL_ONLY 表的 dirty_outbox 记录应预设 processed=1 + local_only=1。"""
        store = cache_store
        # tasks 是 LOCAL_ONLY 表
        rid = await store.add_dirty_outbox(
            "tasks", "task-001", "upsert", payload='{"status":"pending"}',
        )
        assert rid > 0, "应成功插入 dirty_outbox 记录"

        cursor = await store._db.execute(
            "SELECT processed, local_only FROM dirty_outbox WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row is not None, "记录应存在"
        assert row[0] == 1, "LOCAL_ONLY 表的 dirty_outbox 记录应预设 processed=1"
        assert row[1] == 1, "LOCAL_ONLY 表的 dirty_outbox 记录应预设 local_only=1"

    @pytest.mark.asyncio
    async def test_crdb_table_not_preset_processed(self, cache_store):
        """CRDB 表的 dirty_outbox 记录应 processed=0 + local_only=0(待 crdb_sync 处理)。"""
        store = cache_store
        # file_records 是 CRDB 表
        rid = await store.add_dirty_outbox(
            "file_records", "file-code-001", "upsert",
            payload='{"file_code":"file-code-001"}',
        )
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT processed, local_only FROM dirty_outbox WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 0, "CRDB 表的 dirty_outbox 记录应 processed=0(待处理)"
        assert row[1] == 0, "CRDB 表的 dirty_outbox 记录应 local_only=0"

    @pytest.mark.asyncio
    async def test_archive_only_table_not_preset_processed(self, cache_store):
        """ARCHIVE_ONLY 表的 dirty_outbox 记录应 processed=0 + local_only=0。"""
        store = cache_store
        # audit_log_archive 是 ARCHIVE_ONLY 表
        rid = await store.add_dirty_outbox(
            "audit_log_archive", "log-001", "upsert", payload='{"event":"test"}',
        )
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT processed, local_only FROM dirty_outbox WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        # ARCHIVE_ONLY 表不预标记(由 dispatcher 标记)
        assert row[0] == 0, "ARCHIVE_ONLY 表的 dirty_outbox 记录应 processed=0"
        assert row[1] == 0, "ARCHIVE_ONLY 表的 dirty_outbox 记录应 local_only=0"

    @pytest.mark.asyncio
    async def test_unknown_table_defaults_to_local_only(self, cache_store):
        """R41 P0-6: 未知表名 fail-closed,默认 LOCAL_ONLY 行为(预标记)。"""
        store = cache_store
        # 未知表名(未在 TABLE_REPLICATION_POLICY 中声明)
        rid = await store.add_dirty_outbox(
            "unknown_table_xyz", "pk-001", "upsert", payload='{}',
        )
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT processed, local_only FROM dirty_outbox WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        # fail-closed: 未知表默认 LOCAL_ONLY → 预标记
        assert row[0] == 1, "未知表 fail-closed 应预设 processed=1"
        assert row[1] == 1, "未知表 fail-closed 应预设 local_only=1"

    @pytest.mark.asyncio
    async def test_local_only_records_not_picked_by_get_dirty_batch(self, cache_store):
        """LOCAL_ONLY 表预标记后,get_dirty_outbox_batch 不应拉取它们(processed=0 过滤)。"""
        store = cache_store
        # 写入 3 条 LOCAL_ONLY 表记录(预设 processed=1)
        for i in range(3):
            await store.add_dirty_outbox(
                "tasks", f"task-{i}", "upsert", payload='{}',
            )
        # 写入 2 条 CRDB 表记录(预设 processed=0)
        for i in range(2):
            await store.add_dirty_outbox(
                "file_records", f"file-{i}", "upsert", payload='{}',
            )

        # get_dirty_outbox_batch 只应返回 processed=0 的记录(CRDB 表)
        batch = await store.get_dirty_outbox_batch(limit=100)
        assert len(batch) == 2, (
            f"应只返回 2 条 CRDB 表记录,实际返回 {len(batch)} 条"
        )
        for r in batch:
            assert r["table_name"] == "file_records", (
                f"批次中不应包含 LOCAL_ONLY 表记录,实际包含: {r['table_name']}"
            )


# ════════════════════════════════════════════════════════════════
# 场景 3: CRDB tombstone handler 覆盖完整性
# ════════════════════════════════════════════════════════════════

class TestCrdbTombstoneHandler:
    """R41 P0-6: crdb_sync_service 中 tombstone handler 覆盖完整性。"""

    def test_tombstone_handlers_dict_exists(self):
        """_DIRTY_OUTBOX_TOMBSTONE_HANDLERS 字典应存在。"""
        from services.crdb_sync_service import _DIRTY_OUTBOX_TOMBSTONE_HANDLERS

        assert isinstance(_DIRTY_OUTBOX_TOMBSTONE_HANDLERS, dict)
        assert len(_DIRTY_OUTBOX_TOMBSTONE_HANDLERS) > 0

    def test_tombstone_handlers_cover_tables_with_upsert_handlers(self):
        """R41 P0-6: 所有有 upsert handler 的表都应有 tombstone handler(一致性)。

        CRDB 表中部分表(decode_logs/channels/spare_pool 等)无 upsert handler,
        这些表的 dirty_outbox 记录会路由到 DLQ(由 dispatcher 校验)。
        但有 upsert handler 的表必须同时有 tombstone handler,否则软删除会丢失。
        """
        from services.crdb_sync_service import (
            _DIRTY_OUTBOX_TABLE_HANDLERS,
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
        )

        # 有 upsert handler 的表必须也有 tombstone handler(一致性)
        tables_with_upsert = set(_DIRTY_OUTBOX_TABLE_HANDLERS.keys())
        tables_with_tombstone = set(_DIRTY_OUTBOX_TOMBSTONE_HANDLERS.keys())
        missing = tables_with_upsert - tables_with_tombstone
        assert not missing, (
            f"以下表有 upsert handler 但缺 tombstone handler(软删除会丢失): {missing}"
        )

    def test_tombstone_pk_columns_dict_exists(self):
        """_TOMBSTONE_PK_COLUMNS 字典应存在。"""
        from services.crdb_sync_service import _TOMBSTONE_PK_COLUMNS

        assert isinstance(_TOMBSTONE_PK_COLUMNS, dict)
        assert len(_TOMBSTONE_PK_COLUMNS) > 0

    def test_tombstone_pk_columns_cover_tables_with_handlers(self):
        """R41 P0-6: 所有有 tombstone handler 的表都应有 pk column(一致性)。"""
        from services.crdb_sync_service import (
            _TOMBSTONE_PK_COLUMNS,
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
        )

        tables_with_tombstone = set(_DIRTY_OUTBOX_TOMBSTONE_HANDLERS.keys())
        tables_with_pk = set(_TOMBSTONE_PK_COLUMNS.keys())
        missing = tables_with_tombstone - tables_with_pk
        assert not missing, (
            f"以下表有 tombstone handler 但缺 pk column: {missing}"
        )

    def test_tombstone_pk_columns_correct(self):
        """tombstone pk column 应与表的主键列名一致。"""
        from services.crdb_sync_service import _TOMBSTONE_PK_COLUMNS

        expected_pk = {
            "file_records": "file_code",
            "codes": "code",
            "users": "user_id",
            "jobs": "id",
            "cells": "slot_id",
        }
        for table, pk_col in expected_pk.items():
            assert _TOMBSTONE_PK_COLUMNS.get(table) == pk_col, (
                f"表 {table} 的 tombstone pk column 应为 {pk_col}, "
                f"实际: {_TOMBSTONE_PK_COLUMNS.get(table)}"
            )

    def test_dispatch_crdb_tombstone_function_exists(self):
        """_dispatch_crdb_tombstone 函数应存在且可调用。"""
        from services.crdb_sync_service import _dispatch_crdb_tombstone

        assert callable(_dispatch_crdb_tombstone), (
            "_dispatch_crdb_tombstone 应是可调用函数"
        )

    @pytest.mark.asyncio
    async def test_dispatch_crdb_tombstone_deletes_by_pk(self):
        """R46 P1: _dispatch_crdb_tombstone 在 schema 探测失败(mock 环境 _client.is_connected=False)
        时 fail-closed 路由到 DLQ,不执行 hard delete。

        R46 P1-c 整改前: fallback 立即执行 DELETE FROM <table> WHERE <pk_col>=$1
        R46 P1-c 整改后: schema 未确认时 fail-closed 路由到 DLQ,hard delete 只能由
                        retention worker 在备份保留窗口后执行
        """
        # Mock database.session 提供 D1Collection
        mock_col = MagicMock(name="mock_users_col")
        mock_col.execute_raw = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session.get_users_col = MagicMock(return_value=mock_col)
        mock_session.get_file_records_col = MagicMock(return_value=mock_col)
        mock_session.get_codes_col = MagicMock(return_value=mock_col)
        mock_session.get_jobs_col = MagicMock(return_value=mock_col)
        mock_session.get_cells_col = MagicMock(return_value=mock_col)
        # 注入 sys.modules
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session

        # R46 P1: mock _route_dirty_outbox_to_dlq 验证 DLQ 路由
        dlq_calls = []

        async def mock_route_dlq(table_name, recs, error_msg):
            dlq_calls.append((table_name, recs, error_msg))
            # R51 P0-9: _route_dirty_outbox_to_dlq 返回 dict(非 list/None)
            return {"success": True, "failed_ids": [], "error": ""}

        try:
            # 重新导入 crdb_sync_service(若已导入则用现有模块)
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            records = [
                {"id": 1, "pk": "user-123", "operation": "tombstone"},
                {"id": 2, "pk": "user-456", "operation": "tombstone"},
            ]
            with patch.object(
                css, "_route_dirty_outbox_to_dlq",
                side_effect=mock_route_dlq,
            ):
                ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")
            # R46 P1: 返回所有 id 让 _sync_dirty_outbox 标记为 processed(已在 DLQ)
            assert ids == [1, 2], f"应返回所有 id(已路由到 DLQ),实际 {ids}"

            # R46 P1: 应路由到 DLQ 1 次,表名匹配,记录数匹配
            assert len(dlq_calls) == 1
            assert dlq_calls[0][0] == "users"
            assert dlq_calls[0][1] == records
            assert "tombstone schema probe failed" in dlq_calls[0][2]
            # R46 P1: 不应执行任何 DELETE FROM(fail-closed,hard delete 由 retention worker 执行)
            assert mock_col.execute_raw.call_count == 0
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 4: jobs/cells upsert handler
# ════════════════════════════════════════════════════════════════

class TestJobsCellsUpsertHandler:
    """R41 P0-6: crdb_sync_service 中 jobs/cells 的 upsert handler。"""

    def test_dispatch_jobs_upsert_function_exists(self):
        """_dispatch_jobs_upsert 函数应存在且可调用。"""
        from services.crdb_sync_service import _dispatch_jobs_upsert

        assert callable(_dispatch_jobs_upsert), (
            "_dispatch_jobs_upsert 应是可调用函数"
        )

    def test_dispatch_cells_upsert_function_exists(self):
        """_dispatch_cells_upsert 函数应存在且可调用。"""
        from services.crdb_sync_service import _dispatch_cells_upsert

        assert callable(_dispatch_cells_upsert), (
            "_dispatch_cells_upsert 应是可调用函数"
        )

    def test_table_handlers_includes_jobs_and_cells(self):
        """_DIRTY_OUTBOX_TABLE_HANDLERS 应包含 jobs 和 cells 的 upsert handler。"""
        from services.crdb_sync_service import (
            _DIRTY_OUTBOX_TABLE_HANDLERS,
            _dispatch_jobs_upsert,
            _dispatch_cells_upsert,
        )

        assert "jobs" in _DIRTY_OUTBOX_TABLE_HANDLERS, (
            "_DIRTY_OUTBOX_TABLE_HANDLERS 应包含 jobs"
        )
        assert "cells" in _DIRTY_OUTBOX_TABLE_HANDLERS, (
            "_DIRTY_OUTBOX_TABLE_HANDLERS 应包含 cells"
        )
        assert _DIRTY_OUTBOX_TABLE_HANDLERS["jobs"] is _dispatch_jobs_upsert
        assert _DIRTY_OUTBOX_TABLE_HANDLERS["cells"] is _dispatch_cells_upsert

    def test_delegated_tables_set_is_empty(self):
        """R41 P0-6: _DIRTY_OUTBOX_TABLES_DELEGATED 应为空集(jobs/cells 已有 explicit handler)。"""
        from services.crdb_sync_service import _DIRTY_OUTBOX_TABLES_DELEGATED

        # R41 P0-6: 此前依赖专用循环,现已有 explicit handler,集合应为空
        assert _DIRTY_OUTBOX_TABLES_DELEGATED == set(), (
            f"_DIRTY_OUTBOX_TABLES_DELEGATED 应为空集, "
            f"实际: {_DIRTY_OUTBOX_TABLES_DELEGATED}"
        )

    @pytest.mark.asyncio
    async def test_dispatch_jobs_upsert_calls_execute_raw(self):
        """_dispatch_jobs_upsert 应调用 D1Collection.execute_raw 执行 INSERT ON CONFLICT。"""
        mock_col = MagicMock(name="mock_jobs_col")
        mock_col.execute_raw = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session.get_jobs_col = MagicMock(return_value=mock_col)
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            records = [
                {
                    "id": 1, "pk": "job-1", "operation": "upsert",
                    "payload": json.dumps({
                        "code": "CODE-001", "target_user_id": 12345,
                        "storage_channel_id": -100123, "storage_msg_ids": "[1,2]",
                        "batch_file_meta": "{}", "task_type": "single",
                        "status": "pending", "created_at": "2026-07-13",
                        "dispatched_at": None,
                    }),
                },
            ]
            ids = await css._dispatch_jobs_upsert(records)
            assert ids == [1], f"应返回成功处理的 id,实际 {ids}"
            assert mock_col.execute_raw.call_count == 1
            sql_arg = mock_col.execute_raw.call_args[0][0]
            assert "INSERT INTO jobs" in sql_arg
            assert "ON CONFLICT" in sql_arg
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 5: dispatcher tombstone 集成
# ════════════════════════════════════════════════════════════════

class TestDispatcherTombstoneIntegration:
    """R41 P0-6: _dispatch_dirty_outbox_to_crdb 路由 tombstone 记录到 tombstone handler。"""

    @pytest.mark.asyncio
    async def test_dispatcher_routes_tombstone_to_tombstone_handler(self):
        """R46 P1: dispatcher 应将 tombstone operation 路由到 _dispatch_crdb_tombstone。

        R46 P1-c 整改后,_dispatch_crdb_tombstone 在 mock 环境(schema 探测失败)会
        fail-closed 路由到 DLQ;返回 id 列表让 _sync_dirty_outbox 标记为 processed。
        """
        # Mock database.session 提供所有 D1Collection
        mock_col = MagicMock(name="mock_col")
        mock_col.execute_raw = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session.get_users_col = MagicMock(return_value=mock_col)
        mock_session.get_file_records_col = MagicMock(return_value=mock_col)
        mock_session.get_codes_col = MagicMock(return_value=mock_col)
        mock_session.get_jobs_col = MagicMock(return_value=mock_col)
        mock_session.get_cells_col = MagicMock(return_value=mock_col)
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            # file_records 表的 tombstone 记录
            records = [
                {"id": 101, "pk": "file-code-001", "operation": "tombstone"},
                {"id": 102, "pk": "file-code-002", "operation": "tombstone"},
            ]
            # R46 P1: mock 环境 _client.is_connected=False,schema 探测返回 None,
            # _dispatch_crdb_tombstone 走 DLQ 路径,返回所有 id 标记为 processed
            # R51 P0-9: mock _route_dirty_outbox_to_dlq 返回 dict(非 None)
            async def mock_route_dlq(table, recs, reason):
                return {"success": True, "failed_ids": [], "error": ""}
            with patch.object(
                css, "_route_dirty_outbox_to_dlq", side_effect=mock_route_dlq,
            ):
                ids = await css._dispatch_dirty_outbox_to_crdb("file_records", records)
            # 应返回成功处理的 id(已 dispatch 到 tombstone handler,handler 路由到 DLQ)
            assert 101 in ids, "tombstone 记录 id=101 应被处理(路由到 DLQ)"
            assert 102 in ids, "tombstone 记录 id=102 应被处理(路由到 DLQ)"
            # R46 P1: 不应执行任何 DELETE FROM(fail-closed)
            assert mock_col.execute_raw.call_count == 0
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_dispatcher_mixed_upsert_and_tombstone(self):
        """dispatcher 应能处理同一表中混合的 upsert + tombstone 记录。"""
        mock_col = MagicMock(name="mock_col")
        mock_col.execute_raw = AsyncMock(return_value=None)

        # _dispatch_crdb_tombstone 在函数顶部 import 所有 5 个 collection accessor,
        # 必须全部 mock 否则 ImportError
        mock_session = types.ModuleType("database.session")
        mock_session.get_users_col = MagicMock(return_value=mock_col)
        mock_session.get_file_records_col = MagicMock(return_value=mock_col)
        mock_session.get_codes_col = MagicMock(return_value=mock_col)
        mock_session.get_jobs_col = MagicMock(return_value=mock_col)
        mock_session.get_cells_col = MagicMock(return_value=mock_col)
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            records = [
                {
                    "id": 1, "pk": "file-1", "operation": "upsert",
                    "payload": json.dumps({
                        "file_code": "file-1", "uploader_id": 100,
                        "primary_channel_id": -100, "primary_channel_msg_id": 1,
                        "file_types": "doc", "backup_channel_msg_ids": "[]",
                        "batch_msg_ids": "[]", "batch_file_meta": "{}",
                        "file_ids": "[]", "status": "active",
                        "request_count": 0, "create_time": "2026-07-13",
                        "expire_time": None,
                    }),
                },
                {"id": 2, "pk": "file-2", "operation": "tombstone"},
            ]
            # R51 P0-9: mock _route_dirty_outbox_to_dlq 返回 dict(非 None)
            # tombstone 记录在 mock 环境 schema 探测失败时走 DLQ 路径
            async def mock_route_dlq(table, recs, reason):
                return {"success": True, "failed_ids": [], "error": ""}
            with patch.object(
                css, "_route_dirty_outbox_to_dlq", side_effect=mock_route_dlq,
            ):
                ids = await css._dispatch_dirty_outbox_to_crdb("file_records", records)
            # 两条记录都应被处理
            assert 1 in ids, "upsert 记录应被处理"
            assert 2 in ids, "tombstone 记录应被处理"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_dispatcher_invalid_operation_routed_to_dlq(self, cache_store):
        """dispatcher 应将未知 operation(非 upsert/tombstone)路由到 DLQ。"""
        store = cache_store
        # 直接调用 _dispatch_dirty_outbox_to_crdb,传入未知 operation
        from services.crdb_sync_service import _dispatch_dirty_outbox_to_crdb

        # Mock database.session(避免导入 D1Collection 失败)
        mock_col = MagicMock()
        mock_col.execute_raw = AsyncMock(return_value=None)
        mock_session = types.ModuleType("database.session")
        mock_session.get_file_records_col = MagicMock(return_value=mock_col)
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            records = [
                {"id": 1, "pk": "file-1", "operation": "invalid_op"},
            ]
            # 调用 dispatcher — 未知 operation 不应抛异常,应路由到 DLQ
            ids = await css._dispatch_dirty_outbox_to_crdb("file_records", records)
            # dead_records 已路由到 DLQ,其 id 应在返回列表中(避免重复 dispatch)
            assert 1 in ids, "未知 operation 的记录 id 应在返回列表中(已路由到 DLQ)"

            # 验证 DLQ 记录已写入
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM dlq_records WHERE table_name = 'file_records'"
            )
            row = await cursor.fetchone()
            assert row[0] >= 1, "未知 operation 应路由到 dlq_records 表"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 6: dlq_records 表结构 + cleanup_dlq
# ════════════════════════════════════════════════════════════════

class TestDlqRecordsTable:
    """R41 P0-6: dlq_records 表结构 + insert_dlq_record / cleanup_dlq / list_dlq_records。"""

    @pytest.mark.asyncio
    async def test_dlq_records_table_exists(self, cache_store):
        """dlq_records 表应在 init 后存在。"""
        store = cache_store
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dlq_records'"
        )
        row = await cursor.fetchone()
        assert row is not None, "dlq_records 表应存在"
        assert row[0] == "dlq_records"

    @pytest.mark.asyncio
    async def test_dlq_records_table_has_required_columns(self, cache_store):
        """dlq_records 表应包含所有必需字段。"""
        store = cache_store
        cursor = await store._db.execute("PRAGMA table_info(dlq_records)")
        rows = await cursor.fetchall()
        col_names = {row[1] for row in rows}
        required = {
            "id", "message_id", "table_name", "reason", "status",
            "retry_count", "max_retries", "next_retry_at", "last_error",
            "original", "created_at", "updated_at",
        }
        missing = required - col_names
        assert not missing, f"dlq_records 表缺失字段: {missing}"

    @pytest.mark.asyncio
    async def test_insert_dlq_record_creates_new_record(self, cache_store):
        """insert_dlq_record 应创建新的 DLQ 记录。"""
        store = cache_store
        rid = await store.insert_dlq_record(
            message_id="dirty_outbox:42",
            table_name="file_records",
            reason="CRDB 写入失败",
            original={"id": 42, "pk": "file-1"},
            max_retries=5,
            next_retry_at="2026-07-13T12:00:00",
        )
        assert rid > 0, "应返回新记录 id"

        cursor = await store._db.execute(
            "SELECT message_id, table_name, reason, status, retry_count, "
            "max_retries, next_retry_at, last_error, original "
            "FROM dlq_records WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "dirty_outbox:42"
        assert row[1] == "file_records"
        assert "CRDB 写入失败" in row[2]
        assert row[3] == "pending"
        assert row[4] == 0, "新记录 retry_count 应为 0"
        assert row[5] == 5
        assert row[6] == "2026-07-13T12:00:00"
        assert row[7] is not None, "last_error 应记录失败原因"
        assert row[8] is not None, "original 应为 JSON 字符串"

    @pytest.mark.asyncio
    async def test_insert_dlq_record_dedup_by_message_id(self, cache_store):
        """相同 message_id 重复插入应累加 retry_count(幂等去重)。"""
        store = cache_store
        rid1 = await store.insert_dlq_record(
            message_id="dirty_outbox:99",
            table_name="codes",
            reason="第一次失败",
            max_retries=5,
            next_retry_at="2026-07-13T12:00:00",
        )
        rid2 = await store.insert_dlq_record(
            message_id="dirty_outbox:99",
            table_name="codes",
            reason="第二次失败",
            max_retries=5,
            next_retry_at="2026-07-13T12:01:00",
        )
        assert rid1 == rid2, "相同 message_id 应返回相同 id(幂等去重)"

        cursor = await store._db.execute(
            "SELECT retry_count, last_error FROM dlq_records WHERE id = ?",
            (rid1,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "重复插入应累加 retry_count 到 1"
        assert "第二次失败" in row[1], "last_error 应更新为最新错误"

    @pytest.mark.asyncio
    async def test_insert_dlq_record_marks_permanently_failed_at_max_retries(self, cache_store):
        """达到 max_retries 时,insert_dlq_record 应标记为 permanently_failed。"""
        store = cache_store
        max_retries = 3
        # R41 P0-6: retry_count 从 0 开始,每次重复插入累加 1。
        # 要使 retry_count 达到 max_retries=3,需要 max_retries+1 次插入:
        #   1st: INSERT 新记录(retry_count=0)
        #   2nd: UPDATE retry_count 0→1
        #   3rd: UPDATE retry_count 1→2
        #   4th: UPDATE retry_count 2→3, 3>=3 → permanently_failed
        rid = 0
        for i in range(max_retries + 1):
            rid = await store.insert_dlq_record(
                message_id="dirty_outbox:perm-fail",
                table_name="users",
                reason=f"第 {i + 1} 次失败",
                max_retries=max_retries,
                next_retry_at="2026-07-13T12:00:00",
            )
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT status, retry_count, next_retry_at FROM dlq_records WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        # 第 4 次插入时 retry_count 从 2 → 3,达到 max_retries=3
        assert row[0] == "permanently_failed", (
            f"达到 max_retries 后应标记 permanently_failed,实际 status={row[0]}"
        )
        assert row[1] == max_retries
        assert row[2] is None, "permanently_failed 记录的 next_retry_at 应为 None"

    @pytest.mark.asyncio
    async def test_cleanup_dlq_marks_records_as_permanently_failed(self, cache_store):
        """cleanup_dlq 应将 retry_count >= max_retries 的记录标记为 permanently_failed。"""
        store = cache_store
        # 手动插入一条 retry_count = max_retries 的记录(status=pending)
        await store._db.execute(
            """INSERT INTO dlq_records
               (message_id, table_name, reason, status, retry_count,
                max_retries, next_retry_at, last_error, original,
                created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 5, 5, '2026-07-13', 'err', '{}',
                       '2026-07-13', '2026-07-13')""",
            ("dirty_outbox:cleanup-1", "codes", "test cleanup"),
        )
        await store._db.commit()

        affected = await store.cleanup_dlq()
        assert affected >= 1, "应标记至少 1 条记录为 permanently_failed"

        cursor = await store._db.execute(
            "SELECT status FROM dlq_records WHERE message_id = ?",
            ("dirty_outbox:cleanup-1",),
        )
        row = await cursor.fetchone()
        assert row[0] == "permanently_failed"

    @pytest.mark.asyncio
    async def test_cleanup_dlq_skips_already_permanently_failed(self, cache_store):
        """cleanup_dlq 不应重复处理已 permanently_failed 的记录。"""
        store = cache_store
        # 插入一条已 permanently_failed 的记录
        await store._db.execute(
            """INSERT INTO dlq_records
               (message_id, table_name, reason, status, retry_count,
                max_retries, next_retry_at, last_error, original,
                created_at, updated_at)
               VALUES (?, ?, ?, 'permanently_failed', 5, 5, NULL, 'err', '{}',
                       '2026-07-13', '2026-07-13')""",
            ("dirty_outbox:already-failed", "codes", "already failed"),
        )
        await store._db.commit()

        affected = await store.cleanup_dlq()
        assert affected == 0, "已 permanently_failed 的记录不应被重复处理"

    @pytest.mark.asyncio
    async def test_list_dlq_records_returns_all(self, cache_store):
        """list_dlq_records() 不传 status 时返回所有记录。"""
        store = cache_store
        for i in range(3):
            await store.insert_dlq_record(
                message_id=f"dirty_outbox:list-{i}",
                table_name="codes",
                reason=f"reason-{i}",
                max_retries=5,
                next_retry_at="2026-07-13T12:00:00",
            )
        records = await store.list_dlq_records(limit=100)
        assert len(records) >= 3, f"应返回至少 3 条记录,实际 {len(records)}"

    @pytest.mark.asyncio
    async def test_list_dlq_records_filters_by_status(self, cache_store):
        """list_dlq_records(status='pending') 只返回 pending 状态记录。"""
        store = cache_store
        # 插入 2 条 pending
        for i in range(2):
            await store.insert_dlq_record(
                message_id=f"dirty_outbox:pending-{i}",
                table_name="codes",
                reason="pending reason",
                max_retries=5,
                next_retry_at="2026-07-13T12:00:00",
            )
        # 插入 1 条 permanently_failed(通过手动 SQL)
        await store._db.execute(
            """INSERT INTO dlq_records
               (message_id, table_name, reason, status, retry_count,
                max_retries, next_retry_at, last_error, original,
                created_at, updated_at)
               VALUES (?, ?, ?, 'permanently_failed', 5, 5, NULL, 'err', '{}',
                       '2026-07-13', '2026-07-13')""",
            ("dirty_outbox:failed-1", "codes", "failed"),
        )
        await store._db.commit()

        pending_records = await store.list_dlq_records(status="pending", limit=100)
        for r in pending_records:
            assert r["status"] == "pending", "list_dlq_records(status='pending') 不应返回非 pending 记录"

    @pytest.mark.asyncio
    async def test_dlq_records_status_machine(self, cache_store):
        """DLQ 状态机: pending → retrying → done / permanently_failed。"""
        store = cache_store
        # 插入新记录(初始 pending)
        rid = await store.insert_dlq_record(
            message_id="dirty_outbox:sm-1",
            table_name="users",
            reason="状态机测试",
            max_retries=2,
            next_retry_at="2026-07-13T12:00:00",
        )
        # 第 2 次插入(retry_count 1, 仍为 pending)
        await store.insert_dlq_record(
            message_id="dirty_outbox:sm-1",
            table_name="users",
            reason="重试 1",
            max_retries=2,
            next_retry_at="2026-07-13T12:01:00",
        )
        cursor = await store._db.execute(
            "SELECT status, retry_count FROM dlq_records WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending", "retry_count=1 < max_retries=2 应仍为 pending"
        assert row[1] == 1

        # 第 3 次插入(retry_count 2 = max_retries, 应标记 permanently_failed)
        await store.insert_dlq_record(
            message_id="dirty_outbox:sm-1",
            table_name="users",
            reason="重试 2 - 达到上限",
            max_retries=2,
            next_retry_at="2026-07-13T12:02:00",
        )
        cursor = await store._db.execute(
            "SELECT status, retry_count, next_retry_at FROM dlq_records WHERE id = ?",
            (rid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "permanently_failed", (
            "retry_count=2 = max_retries=2 应标记 permanently_failed"
        )
        assert row[1] == 2
        assert row[2] is None, "permanently_failed 记录 next_retry_at 应为 None"


# ════════════════════════════════════════════════════════════════
# 场景 7: CRDB 表缺失 handler 路由到 DLQ
# ════════════════════════════════════════════════════════════════

class TestCrdbMissingHandlerRoutesToDlq:
    """R41 P0-6: CRDB 表缺失 handler(upsert/tombstone)时路由到 DLQ,不静默丢弃。"""

    @pytest.mark.asyncio
    async def test_crdb_table_missing_upsert_handler_routes_to_dlq(
        self, cache_store, monkeypatch,
    ):
        """CRDB 表缺失 upsert handler 时,记录应路由到 DLQ。"""
        store = cache_store
        # Mock database.session(避免导入 D1Collection 失败)
        mock_col = MagicMock()
        mock_col.execute_raw = AsyncMock(return_value=None)
        mock_session = types.ModuleType("database.session")
        mock_session.get_file_records_col = MagicMock(return_value=mock_col)
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            # 篡改 _DIRTY_OUTBOX_TABLE_HANDLERS 临时移除 file_records 的 upsert handler
            original_handlers = dict(css._DIRTY_OUTBOX_TABLE_HANDLERS)
            try:
                # 删除 file_records 的 upsert handler
                css._DIRTY_OUTBOX_TABLE_HANDLERS.pop("file_records", None)
                records = [
                    {"id": 1, "pk": "file-1", "operation": "upsert", "payload": "{}"},
                ]
                ids = await css._dispatch_dirty_outbox_to_crdb("file_records", records)
                # 应返回所有 id(已路由到 DLQ,避免重复 dispatch)
                assert 1 in ids, "缺失 handler 的记录 id 应在返回列表中"
            finally:
                css._DIRTY_OUTBOX_TABLE_HANDLERS.clear()
                css._DIRTY_OUTBOX_TABLE_HANDLERS.update(original_handlers)

            # 验证 DLQ 记录已写入
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM dlq_records WHERE table_name = 'file_records'"
            )
            row = await cursor.fetchone()
            assert row[0] >= 1, "缺失 upsert handler 的 CRDB 表记录应路由到 dlq_records"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_crdb_table_missing_tombstone_handler_routes_to_dlq(
        self, cache_store, monkeypatch,
    ):
        """CRDB 表缺失 tombstone handler 时,记录应路由到 DLQ。"""
        store = cache_store
        mock_col = MagicMock()
        mock_col.execute_raw = AsyncMock(return_value=None)
        mock_session = types.ModuleType("database.session")
        mock_session.get_file_records_col = MagicMock(return_value=mock_col)
        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)

            # 篡改 _DIRTY_OUTBOX_TOMBSTONE_HANDLERS 临时移除 file_records
            original_tomb = dict(css._DIRTY_OUTBOX_TOMBSTONE_HANDLERS)
            original_pk = dict(css._TOMBSTONE_PK_COLUMNS)
            try:
                css._DIRTY_OUTBOX_TOMBSTONE_HANDLERS.pop("file_records", None)
                records = [
                    {"id": 1, "pk": "file-1", "operation": "tombstone"},
                ]
                ids = await css._dispatch_dirty_outbox_to_crdb("file_records", records)
                assert 1 in ids, "缺失 tombstone handler 的记录 id 应在返回列表中"
            finally:
                css._DIRTY_OUTBOX_TOMBSTONE_HANDLERS.clear()
                css._DIRTY_OUTBOX_TOMBSTONE_HANDLERS.update(original_tomb)
                css._TOMBSTONE_PK_COLUMNS.clear()
                css._TOMBSTONE_PK_COLUMNS.update(original_pk)

            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM dlq_records WHERE table_name = 'file_records'"
            )
            row = await cursor.fetchone()
            assert row[0] >= 1, "缺失 tombstone handler 的 CRDB 表记录应路由到 dlq_records"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)
