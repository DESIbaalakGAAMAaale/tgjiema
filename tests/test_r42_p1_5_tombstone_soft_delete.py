"""R42 P1-5 整改测试: Tombstone soft-delete + retention 物理清理。

测试覆盖 5 大场景:
1. _dispatch_crdb_tombstone soft_delete 路径(UPDATE deleted_at + is_tombstone=1)
2. _dispatch_crdb_tombstone fallback 路径(DELETE + audit_log)
3. _is_crdb_table_supports_soft_delete 探测 + 缓存
4. get_tombstone_policy 策略推断(CRDB → soft_delete,LOCAL_ONLY → no_tombstone)
5. retention_worker(cleanup_hard_delete / run_retention_job / retention_cleanup_job)
6. r40_scheduler 注册 retention_cleanup_job

测试策略:
- 真实 SQLite cache_store 用于 audit_log 验证
- Mock database.session(D1Collection / _client)用于 CRDB 操作 mock
- 源码静态扫描用于 r40_scheduler 注册校验
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


# 测试环境兼容: mock telegram / telegram.ext
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 辅助: 临时 SQLite cache_store fixture
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(R42 P1-5 测试用)。

    使用临时目录避免污染开发环境,测试结束后自动清理。
    同时替换全局单例 _store,使 crdb_sync_service._get_cache_store_safe()
    能返回此实例。
    """
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r42_p1_5_test_")
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


def _setup_mock_session_with_soft_delete(supports: bool = True):
    """构造 mock database.session 模块,控制 _is_crdb_table_supports_soft_delete 行为。

    Args:
        supports: True 时让 _client.fetch 返回 ['deleted_at', 'is_tombstone'],
                  False 时返回空列表(不支持 soft_delete)。

    Returns:
        (mock_session_module, mock_col) — mock_col 是 D1Collection mock
    """
    mock_col = MagicMock(name="mock_col")
    mock_col.execute_raw = AsyncMock(return_value=None)

    mock_session = types.ModuleType("database.session")
    mock_session.get_users_col = MagicMock(return_value=mock_col)
    mock_session.get_file_records_col = MagicMock(return_value=mock_col)
    mock_session.get_codes_col = MagicMock(return_value=mock_col)
    mock_session.get_jobs_col = MagicMock(return_value=mock_col)
    mock_session.get_cells_col = MagicMock(return_value=mock_col)

    # 模拟 _client(用于 _is_crdb_table_supports_soft_delete 查询 information_schema)
    mock_client = MagicMock(name="mock_client")
    mock_client.is_connected = True
    if supports:
        # 返回 deleted_at 和 is_tombstone 两列
        mock_client.fetch = AsyncMock(return_value=[("deleted_at",), ("is_tombstone",)])
    else:
        # 返回空(表无这两列)
        mock_client.fetch = AsyncMock(return_value=[])
    mock_session._client = mock_client

    return mock_session, mock_col


# ════════════════════════════════════════════════════════════════
# 场景 1: _dispatch_crdb_tombstone soft_delete 路径
# ════════════════════════════════════════════════════════════════

class TestDispatchCrdbTombstoneSoftDelete:
    """R42 P1-5: _dispatch_crdb_tombstone 在表支持 soft_delete 时走 UPDATE 路径。"""

    @pytest.mark.asyncio
    async def test_dispatch_tombstone_uses_update_when_soft_delete_supported(self):
        """表支持 soft_delete 时应使用 UPDATE 而非 DELETE。"""
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            # 重置缓存(避免上一个测试遗留)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 1, "pk": "user-001", "operation": "tombstone",
                    "payload": json.dumps({"user_id": 1, "deleted_at": "2026-07-13T00:00:00"}),
                    "created_at": "2026-07-13T00:00:00",
                },
            ]
            ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")

            assert ids == [1]
            # 验证 execute_raw 被调用,SQL 为 UPDATE
            assert mock_col.execute_raw.call_count == 1
            sql = mock_col.execute_raw.call_args[0][0]
            assert "UPDATE users SET" in sql, f"应使用 UPDATE,实际: {sql}"
            assert "deleted_at" in sql
            assert "is_tombstone" in sql
            assert "WHERE user_id" in sql
            # 验证不包含 DELETE
            assert "DELETE FROM" not in sql
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_dispatch_tombstone_sets_deleted_at_and_is_tombstone(self):
        """UPDATE 应同时设置 deleted_at 和 is_tombstone=1。"""
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 101, "pk": "user-101", "operation": "tombstone",
                    "payload": json.dumps({"deleted_at": "2026-07-13T12:00:00"}),
                    "created_at": "2026-07-13T12:00:00",
                },
            ]
            await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # 验证 SQL 包含 deleted_at 赋值和 is_tombstone=1
            sql = mock_col.execute_raw.call_args[0][0]
            assert "deleted_at = $1" in sql or "deleted_at =$1" in sql, (
                f"SQL 应包含 'deleted_at = $1',实际: {sql}"
            )
            assert "is_tombstone = 1" in sql or "is_tombstone=1" in sql, (
                f"SQL 应包含 'is_tombstone = 1',实际: {sql}"
            )

            # R46 P1: 验证参数:[deleted_at, version, pk] — R46 P1 整改后 UPDATE 携带 version
            # (store.allocate_version 分配,通过 INSERT ON CONFLICT 原子递增)
            args = mock_col.execute_raw.call_args[0][1]
            assert len(args) == 3, (
                f"R46 P1: UPDATE 应携带 3 个参数 [deleted_at, version, pk],实际 {len(args)} 个: {args}"
            )
            # 第一个参数为 deleted_at 时间戳(从 payload 提取)
            assert "2026-07-13" in str(args[0])
            # 第二个参数为 version(R46 P1: store.allocate_version 分配)
            assert isinstance(args[1], (int, str)), (
                f"version 应为 int 或 str,实际: {type(args[1])}"
            )
            # 第三个参数为 pk
            assert args[2] == "user-101"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_dispatch_tombstone_preserves_deleted_at_for_backup_recovery(self):
        """tombstone 保留 deleted_at 供备份恢复使用(不立即物理删除)。"""
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            # tombstone 记录携带原始 deleted_at 时间戳
            original_deleted_at = "2026-06-01T08:30:00"
            records = [
                {
                    "id": 200, "pk": "user-200", "operation": "tombstone",
                    "payload": json.dumps({"deleted_at": original_deleted_at}),
                    "created_at": "2026-07-13T00:00:00",
                },
            ]
            await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # 验证 SQL 是 UPDATE(保留行,不删除)
            sql = mock_col.execute_raw.call_args[0][0]
            assert sql.startswith("UPDATE"), (
                f"应使用 UPDATE 保留 tombstone,实际: {sql}"
            )

            # 验证 deleted_at 参数来自 payload(原始时间戳)
            args = mock_col.execute_raw.call_args[0][1]
            assert args[0] == original_deleted_at, (
                f"UPDATE 应使用 payload 中的 deleted_at={original_deleted_at},"
                f"实际: {args[0]}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 2: _dispatch_crdb_tombstone fallback 路径(DELETE + audit_log)
# ════════════════════════════════════════════════════════════════

class TestDispatchCrdbTombstoneFallback:
    """R42 P1-5: _dispatch_crdb_tombstone 在表不支持 soft_delete 时 fallback 到 DELETE。"""

    @pytest.mark.asyncio
    async def test_dispatch_tombstone_fallback_to_delete_when_unsupported(
        self, cache_store,
    ):
        """R46 P1-c: 表不支持 soft_delete 时 fail-closed 路由到 DLQ(不执行 hard delete)。

        整改前: fallback 立即执行 DELETE FROM + audit_log
        整改后: hard delete 只能由 retention worker 在备份保留窗口后执行,
                crdb_sync 直接路由到 DLQ,避免数据不可恢复
        """
        store = cache_store
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=False)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session

        # R46 P1: mock _route_dirty_outbox_to_dlq 验证 DLQ 路由
        dlq_calls = []

        async def mock_route_dlq(table_name, recs, error_msg):
            dlq_calls.append((table_name, recs, error_msg))

        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 1, "pk": "user-001", "operation": "tombstone",
                    "payload": None, "created_at": "2026-07-13T00:00:00",
                },
            ]
            with patch.object(
                css, "_route_dirty_outbox_to_dlq",
                side_effect=mock_route_dlq,
            ):
                ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # R46 P1: 返回所有 id 让 _sync_dirty_outbox 标记为 processed(已在 DLQ)
            assert ids == [1]
            # R46 P1: 应路由到 DLQ(1 次,表名匹配,记录数匹配)
            assert len(dlq_calls) == 1
            assert dlq_calls[0][0] == "users"
            assert dlq_calls[0][1] == records
            assert "does not support soft_delete" in dlq_calls[0][2]
            # R46 P1: 不应执行任何 DELETE FROM(fail-closed,hard delete 由 retention worker 执行)
            assert mock_col.execute_raw.call_count == 0
            # R46 P1: 不应写 hard_delete_fallback audit_log(已废弃,改由 retention worker)
            cursor = await store._db.execute(
                "SELECT action, target_type, target_id FROM audit_log "
                "WHERE action = 'tombstone_hard_delete_fallback' "
                "AND target_type = 'users' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            assert row is None, (
                "R46 P1: 不应写 hard_delete_fallback audit_log(hard delete 由 retention worker 执行)"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 3: _is_crdb_table_supports_soft_delete 探测 + 缓存
# ════════════════════════════════════════════════════════════════

class TestIsCrdbTableSupportsSoftDelete:
    """R42 P1-5: _is_crdb_table_supports_soft_delete 探测与缓存。"""

    @pytest.mark.asyncio
    async def test_supports_soft_delete_detects_columns(self):
        """有 deleted_at + is_tombstone 两列时应返回 True。"""
        mock_session, _ = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            result = await css._is_crdb_table_supports_soft_delete("users")
            assert result is True, "有 deleted_at + is_tombstone 两列应返回 True"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_supports_soft_delete_returns_false_when_missing_columns(self):
        """无 deleted_at 或 is_tombstone 列时应返回 False。"""
        mock_session, _ = _setup_mock_session_with_soft_delete(supports=False)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            result = await css._is_crdb_table_supports_soft_delete("users")
            assert result is False, "无 deleted_at/is_tombstone 列应返回 False"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_supports_soft_delete_caches_result(self):
        """结果应缓存,第二次调用不再查询 information_schema。"""
        mock_session, _ = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            # 第一次调用:查询 information_schema
            result1 = await css._is_crdb_table_supports_soft_delete("users")
            assert result1 is True

            # 检查 _client.fetch 被调用次数
            mock_client = mock_session._client
            assert mock_client.fetch.call_count >= 1

            # 记录第一次调用次数
            first_call_count = mock_client.fetch.call_count

            # 第二次调用:应从缓存读取,不再查询 information_schema
            result2 = await css._is_crdb_table_supports_soft_delete("users")
            assert result2 is True
            # fetch 调用次数不应增加(走缓存)
            assert mock_client.fetch.call_count == first_call_count, (
                "第二次调用应从缓存读取,_client.fetch 不应被再次调用"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_reset_soft_delete_cache_clears_cache(self):
        """_reset_soft_delete_cache 应清空缓存。"""
        mock_session, _ = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            # 第一次调用:查询并缓存
            await css._is_crdb_table_supports_soft_delete("users")
            assert "users" in css._CRDB_SOFT_DELETE_CACHE

            # 重置缓存
            css._reset_soft_delete_cache()
            assert len(css._CRDB_SOFT_DELETE_CACHE) == 0, (
                "重置后缓存应为空"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 4: get_tombstone_policy 策略推断
# ════════════════════════════════════════════════════════════════

class TestGetTombstonePolicy:
    """R42 P1-5: get_tombstone_policy 策略推断。"""

    def test_get_tombstone_policy_default_soft_delete_for_crdb(self):
        """CRDB 同步表应返回 soft_delete。"""
        from services.replication_policy import (
            get_tombstone_policy,
            TOMBSTONE_POLICY_SOFT_DELETE,
        )

        # CRDB 表
        for table in ("users", "file_records", "codes", "cells", "jobs"):
            result = get_tombstone_policy(table)
            assert result == TOMBSTONE_POLICY_SOFT_DELETE, (
                f"CRDB 表 {table} 应返回 soft_delete,实际 {result}"
            )

    def test_get_tombstone_policy_no_tombstone_for_local_only(self):
        """LOCAL_ONLY 表应返回 no_tombstone。"""
        from services.replication_policy import (
            get_tombstone_policy,
            TOMBSTONE_POLICY_NO_TOMBSTONE,
        )

        # LOCAL_ONLY 表
        for table in ("tasks", "audit_log", "approvals", "rbac_roles", "sessions"):
            result = get_tombstone_policy(table)
            assert result == TOMBSTONE_POLICY_NO_TOMBSTONE, (
                f"LOCAL_ONLY 表 {table} 应返回 no_tombstone,实际 {result}"
            )

    def test_get_tombstone_policy_no_tombstone_for_unknown_table(self):
        """未知表(fail-closed)应返回 no_tombstone。"""
        from services.replication_policy import (
            get_tombstone_policy,
            TOMBSTONE_POLICY_NO_TOMBSTONE,
        )

        assert get_tombstone_policy("unknown_table_xyz") == TOMBSTONE_POLICY_NO_TOMBSTONE
        assert get_tombstone_policy("") == TOMBSTONE_POLICY_NO_TOMBSTONE

    def test_get_tombstone_policy_no_tombstone_for_archive_only(self):
        """ARCHIVE_ONLY 表应返回 no_tombstone(不进 CRDB,无 tombstone)。"""
        from services.replication_policy import (
            get_tombstone_policy,
            TOMBSTONE_POLICY_NO_TOMBSTONE,
        )

        assert get_tombstone_policy("audit_log_archive") == TOMBSTONE_POLICY_NO_TOMBSTONE


# ════════════════════════════════════════════════════════════════
# 场景 5: retention_worker(cleanup_hard_delete / run_retention_job)
# ════════════════════════════════════════════════════════════════

class TestRetentionWorkerCleanupHardDelete:
    """R42 P1-5: retention_worker.cleanup_hard_delete 物理删除测试。"""

    @pytest.mark.asyncio
    async def test_cleanup_hard_delete_rejects_when_not_backed_up(
        self, cache_store, monkeypatch,
    ):
        """未备份时 cleanup_hard_delete 应拒绝删除,返回 False。"""
        store = cache_store
        # backup_history 为空(无任何备份)
        await store.set_kv("backup_history", json.dumps([]))

        # Mock crdb_sync_service 的 tombstone handler 映射
        from services.crdb_sync_service import (
            _DIRTY_OUTBOX_TOMBSTONE_HANDLERS,
            _TOMBSTONE_PK_COLUMNS,
        )
        assert "users" in _DIRTY_OUTBOX_TOMBSTONE_HANDLERS
        assert _TOMBSTONE_PK_COLUMNS["users"] == "user_id"

        from services import retention_worker as rw

        result = await rw.cleanup_hard_delete("users", "user-001", retention_days=30)
        assert result is False, "未备份时应拒绝删除,返回 False"

    @pytest.mark.asyncio
    async def test_cleanup_hard_delete_rejects_zero_retention_days(
        self, cache_store,
    ):
        """retention_days <= 0 时应拒绝删除(避免误删近期 tombstone)。"""
        store = cache_store
        # 写入一条成功备份记录
        backup_history = [{
            "backup_id": "backup-001",
            "status": "completed",
            "complete_marker_exists": True,
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T01:00:00",
        }]
        await store.set_kv("backup_history", json.dumps(backup_history))

        from services import retention_worker as rw

        # retention_days=0 应拒绝
        result = await rw.cleanup_hard_delete(
            "users", "user-001", retention_days=0,
        )
        assert result is False, "retention_days=0 应拒绝删除"

    @pytest.mark.asyncio
    async def test_cleanup_hard_delete_deletes_when_backed_up_and_expired(
        self, cache_store, monkeypatch,
    ):
        """已备份且 retention_days > 0 时应执行物理删除,返回 True。"""
        store = cache_store
        # 写入一条成功备份记录
        backup_history = [{
            "backup_id": "backup-001",
            "status": "completed",
            "complete_marker_exists": True,
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T01:00:00",
        }]
        await store.set_kv("backup_history", json.dumps(backup_history))

        # Mock CRDB 客户端
        mock_client = MagicMock(name="mock_client")
        mock_client.is_connected = True
        mock_client.execute = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session._client = mock_client
        # 还需要 collection accessor 用于 _dispatch_crdb_tombstone 等
        # (cleanup_hard_delete 通过 _DIRTY_OUTBOX_TOMBSTONE_HANDLERS 查 crdb_table 名,
        #  实际删除调用 _hard_delete_tombstone_in_crdb 直接走 _client)
        mock_session.get_users_col = MagicMock(return_value=MagicMock())
        mock_session.get_file_records_col = MagicMock(return_value=MagicMock())
        mock_session.get_codes_col = MagicMock(return_value=MagicMock())
        mock_session.get_jobs_col = MagicMock(return_value=MagicMock())
        mock_session.get_cells_col = MagicMock(return_value=MagicMock())

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            from services import retention_worker as rw

            result = await rw.cleanup_hard_delete(
                "users", "user-001", retention_days=30,
            )
            assert result is True, "已备份且超期应删除成功,返回 True"

            # 验证 CRDB DELETE 被调用
            assert mock_client.execute.call_count == 1
            sql = mock_client.execute.call_args[0][0]
            assert "DELETE FROM users" in sql
            assert "WHERE user_id" in sql
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_cleanup_hard_delete_writes_audit_log(
        self, cache_store, monkeypatch,
    ):
        """cleanup_hard_delete 成功删除后应写 audit_log(action=retention_hard_delete)。"""
        store = cache_store
        # 写入成功备份
        backup_history = [{
            "backup_id": "backup-002",
            "status": "completed",
            "complete_marker_exists": True,
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T01:00:00",
        }]
        await store.set_kv("backup_history", json.dumps(backup_history))

        # Mock CRDB 客户端
        mock_client = MagicMock(name="mock_client")
        mock_client.is_connected = True
        mock_client.execute = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session._client = mock_client
        mock_session.get_users_col = MagicMock(return_value=MagicMock())
        mock_session.get_file_records_col = MagicMock(return_value=MagicMock())
        mock_session.get_codes_col = MagicMock(return_value=MagicMock())
        mock_session.get_jobs_col = MagicMock(return_value=MagicMock())
        mock_session.get_cells_col = MagicMock(return_value=MagicMock())

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            from services import retention_worker as rw

            result = await rw.cleanup_hard_delete(
                "users", "user-audit-test", retention_days=30,
            )
            assert result is True

            # 验证 audit_log 写入
            cursor = await store._db.execute(
                "SELECT action, target_type, target_id FROM audit_log "
                "WHERE action = 'retention_hard_delete' "
                "AND target_type = 'users' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            assert row is not None, (
                "应写 audit_log(action=retention_hard_delete)"
            )
            assert row[2] == "user-audit-test"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


class TestRetentionWorkerRunRetentionJob:
    """R42 P1-5: retention_worker.run_retention_job 批量清理测试。"""

    @pytest.mark.asyncio
    async def test_run_retention_job_returns_stats_dict(
        self, cache_store, monkeypatch,
    ):
        """run_retention_job 应返回 {scanned, deleted, errors} 统计字典。"""
        store = cache_store
        # 写入成功备份(确保 cleanup_hard_delete 不会因未备份而拒绝)
        backup_history = [{
            "backup_id": "backup-rj-001",
            "status": "completed",
            "complete_marker_exists": True,
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T01:00:00",
        }]
        await store.set_kv("backup_history", json.dumps(backup_history))

        # Mock CRDB 客户端 + soft_delete 探测返回 True
        mock_client = MagicMock(name="mock_client")
        mock_client.is_connected = True
        # fetch 用于:_query_tombstones_for_table(返回 tombstone 列表)+
        # _is_crdb_table_supports_soft_delete(information_schema 查询)
        # 我们让 fetch 第一次(information_schema)返回 deleted_at+is_tombstone,
        # 第二次(tombstone 查询)返回 2 条 tombstone
        # 但由于 _is_crdb_table_supports_soft_delete 结果会缓存,
        # 后续 information_schema 查询不会再次发起
        # 测试策略:让 fetch 返回满足两者的结果(用 side_effect)
        # 简化:让 fetch 直接返回 2 条 tombstone (假设第一张表是 users)
        # 但 _is_crdb_table_supports_soft_delete 也用 fetch...
        # 改用 side_effect 按顺序返回

        # 第一次 fetch: information_schema 查询(返回 deleted_at + is_tombstone)
        # 后续 fetch: tombstone 查询(返回 2 条)
        info_schema_result = [("deleted_at",), ("is_tombstone",)]
        tombstone_result = [
            ("user-rj-1", "2026-01-01T00:00:00"),
            ("user-rj-2", "2026-01-01T00:00:00"),
        ]
        # 按 _DIRTY_OUTBOX_TOMBSTONE_HANDLERS 顺序: file_records, codes, users, jobs, cells
        # 5 张表,每张表先 fetch information_schema(5 次),再 fetch tombstone
        # 但 information_schema 查询只在第一次表时被调用(后续走缓存)
        # 实际:_is_crdb_table_supports_soft_delete 的 fetch 只调用一次(因缓存)
        # 但 run_retention_job 遍历 5 张表,每张都尝试 _is_crdb_table_supports_soft_delete
        # 已缓存的表跳过 fetch,但第一次仍 fetch

        # 简化测试:让 fetch 总是返回 tombstone_result(2 条)
        # 但 _is_crdb_table_supports_soft_delete 检查的是 column_name IN ('deleted_at', 'is_tombstone')
        # 返回的列名是 column_name,所以我们需要返回 ('deleted_at',) 和 ('is_tombstone',)
        # 让 fetch 按 side_effect 返回:
        # 第 1 次: information_schema → [(deleted_at,), (is_tombstone,)]
        # 第 2+ 次: tombstone query → [(user-rj-1, ts), (user-rj-2, ts)]

        # 但实际上由于 _is_crdb_table_supports_soft_delete 会缓存,每张表只查一次 schema
        # 我们让 fetch 按 side_effect 依次返回

        # 更简单的方式:让 fetch 直接返回 tombstone 列表(2 条),
        # 但 _is_crdb_table_supports_soft_delete 会从 fetch 结果中提取 column_name
        # 检查 'deleted_at' 和 'is_tombstone' 是否在 {r[0] for r in rows}
        # 如果 fetch 返回 [("user-rj-1", "ts"), ("user-rj-2", "ts")],
        # 那 cols = {"user-rj-1", "user-rj-2"},不包含 deleted_at/is_tombstone → False
        # 这样所有表都会被跳过(不支持 soft_delete)→ scanned=0
        # 测试就无法验证批量清理逻辑

        # 改用 side_effect 让 fetch 返回不同结果:
        # - 第一次调用(_is_crdb_table_supports_soft_delete 检查 schema)→ 返回 [(deleted_at,), (is_tombstone,)]
        # - 后续调用(_query_tombstones_for_table 查 tombstone)→ 返回 2 条 tombstone
        call_count = [0]

        async def _mock_fetch(sql, params=None):
            call_count[0] += 1
            # 判断是 information_schema 查询还是 tombstone 查询
            if "information_schema" in sql.lower():
                return [("deleted_at",), ("is_tombstone",)]
            else:
                # tombstone 查询:返回 2 条
                return [
                    ("user-rj-1", "2026-01-01T00:00:00"),
                    ("user-rj-2", "2026-01-01T00:00:00"),
                ]

        mock_client.fetch = _mock_fetch
        mock_client.execute = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session._client = mock_client
        mock_session.get_users_col = MagicMock(return_value=MagicMock())
        mock_session.get_file_records_col = MagicMock(return_value=MagicMock())
        mock_session.get_codes_col = MagicMock(return_value=MagicMock())
        mock_session.get_jobs_col = MagicMock(return_value=MagicMock())
        mock_session.get_cells_col = MagicMock(return_value=MagicMock())

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            from services import retention_worker as rw
            importlib.reload(rw)

            # 重新加载后再次 reset
            css._reset_soft_delete_cache()

            result = await rw.run_retention_job(retention_days=30)

            # 应返回 dict 包含 scanned / deleted / errors
            assert isinstance(result, dict)
            assert "scanned" in result
            assert "deleted" in result
            assert "errors" in result
            # 至少扫描到一些 tombstone(具体数量取决于多少表支持 soft_delete)
            # 由于 mock 让所有表都"支持",scanned 应 > 0
            assert result["scanned"] > 0, (
                f"应扫描到 tombstone 记录,实际 scanned={result['scanned']}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


class TestRetentionCleanupJobFunction:
    """R42 P1-5: retention_cleanup_job 便利函数测试。"""

    @pytest.mark.asyncio
    async def test_retention_cleanup_job_exists_and_callable(self):
        """retention_cleanup_job 便利函数应存在且可调用。"""
        from services.retention_worker import retention_cleanup_job

        assert callable(retention_cleanup_job), (
            "retention_cleanup_job 应是可调用函数"
        )

    @pytest.mark.asyncio
    async def test_retention_cleanup_job_does_not_raise_on_empty_db(
        self, cache_store,
    ):
        """空数据库时 retention_cleanup_job 应正常完成,不抛异常。"""
        store = cache_store
        # 无任何备份(backup_history 为空)
        await store.set_kv("backup_history", json.dumps([]))

        # Mock CRDB 客户端返回空(无 tombstone)
        mock_client = MagicMock(name="mock_client")
        mock_client.is_connected = True
        mock_client.fetch = AsyncMock(return_value=[])
        mock_client.execute = AsyncMock(return_value=None)

        mock_session = types.ModuleType("database.session")
        mock_session._client = mock_client
        mock_session.get_users_col = MagicMock(return_value=MagicMock())
        mock_session.get_file_records_col = MagicMock(return_value=MagicMock())
        mock_session.get_codes_col = MagicMock(return_value=MagicMock())
        mock_session.get_jobs_col = MagicMock(return_value=MagicMock())
        mock_session.get_cells_col = MagicMock(return_value=MagicMock())

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session
        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            from services import retention_worker as rw
            importlib.reload(rw)
            css._reset_soft_delete_cache()

            # 不应抛异常
            await rw.retention_cleanup_job()
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 场景 6: r40_scheduler 注册 retention_cleanup_job
# ════════════════════════════════════════════════════════════════

class TestR40SchedulerRegistersRetentionJob:
    """R42 P1-5: r40_scheduler 应注册 retention_cleanup_job。"""

    def test_r40_scheduler_has_retention_cleanup_job_function(self):
        """r40_scheduler 模块应定义 retention_cleanup_job 函数。"""
        import importlib
        import services.r40_scheduler as scheduler
        importlib.reload(scheduler)

        assert hasattr(scheduler, "retention_cleanup_job"), (
            "r40_scheduler 应定义 retention_cleanup_job 函数"
        )
        assert callable(scheduler.retention_cleanup_job), (
            "retention_cleanup_job 应是可调用函数"
        )

    def test_r40_scheduler_run_scheduler_calls_retention_job(self):
        """run_scheduler 函数源码应包含 retention_cleanup_job 调用。"""
        import inspect
        import services.r40_scheduler as scheduler

        # 检查 run_scheduler 函数源码
        source = inspect.getsource(scheduler.run_scheduler)
        assert "retention_cleanup_job" in source, (
            "run_scheduler 源码应包含 retention_cleanup_job 调用"
        )

    def test_r40_scheduler_docstring_mentions_retention(self):
        """run_scheduler 文档字符串应提及 retention 清理。"""
        import services.r40_scheduler as scheduler

        doc = scheduler.run_scheduler.__doc__ or ""
        assert "retention" in doc.lower() or "tombstone" in doc.lower(), (
            "run_scheduler 文档应提及 retention 或 tombstone 清理"
        )
