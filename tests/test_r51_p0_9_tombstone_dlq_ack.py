"""R51 P0-9: CRDB Tombstone DLQ ACK 测试。

测试覆盖 6 大场景:
1. tombstone 支持 → 直接处理(UPDATE),不进 DLQ
2. tombstone 不支持 → 进 DLQ,SQLite 写入成功 → dirty 标记 processed
3. tombstone 不支持 → 进 DLQ,SQLite 写入失败 → dirty 保持 pending
4. DLQ 写入失败 → 指数退避(next_retry_at 设置)
5. JSONL 写入成功但 SQLite 失败 → dirty 仍保持 pending
6. 多个 dirty IDs 部分成功 → 只有成功的标记 processed

测试策略:
- 真实 SQLite cache_store 用于 dirty_outbox / dlq_records 验证
- Mock database.session(D1Collection / _client)用于 CRDB 操作 mock
- Mock insert_dlq_record 控制 SQLite DLQ 写入成功/失败
- 直接调用 _route_dirty_outbox_to_dlq 验证返回值
- 调用 _dispatch_crdb_tombstone 验证 IDs 返回逻辑
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
    """创建临时文件数据库的 CacheStore 实例(R51 P0-9 测试用)。

    使用临时目录避免污染开发环境,测试结束后自动清理。
    同时替换全局单例 _store,使 crdb_sync_service._get_cache_store_safe()
    能返回此实例。
    """
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r51_p0_9_test_")
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
        mock_client.fetch = AsyncMock(return_value=[("deleted_at",), ("is_tombstone",)])
    else:
        mock_client.fetch = AsyncMock(return_value=[])
    mock_session._client = mock_client

    return mock_session, mock_col


async def _seed_dirty_outbox(store, table_name: str, records: list[dict]) -> list[int]:
    """向 dirty_outbox 表插入测试记录,返回插入的 id 列表。"""
    ids = []
    for r in records:
        cursor = await store._db.execute(
            "INSERT INTO dirty_outbox (table_name, pk, version, operation, payload, created_at, processed) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                table_name,
                r.get("pk", f"pk_{r['id']}"),
                r.get("version", 1),
                r.get("operation", "tombstone"),
                r.get("payload"),
                r.get("created_at", "2026-07-14T10:00:00"),
            ),
        )
        await store._db.commit()
        ids.append(cursor.lastrowid)
    return ids


# ════════════════════════════════════════════════════════════════
# 1. tombstone 支持 → 直接处理(UPDATE),不进 DLQ
# ════════════════════════════════════════════════════════════════

class TestTombstoneSupportedNoDlq:
    """R51 P0-9 场景 1: tombstone 支持时直接处理,不进 DLQ。"""

    @pytest.mark.asyncio
    async def test_tombstone_supported_processes_directly(self, cache_store):
        """表支持 soft_delete 时直接 UPDATE,不调用 _route_dirty_outbox_to_dlq。"""
        store = cache_store
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=True)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session

        dlq_calls = []

        async def mock_route_dlq(table_name, recs, error_msg):
            dlq_calls.append((table_name, recs, error_msg))
            return {"success": True, "failed_ids": [], "error": ""}

        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 1, "pk": "user-001", "operation": "tombstone",
                    "payload": json.dumps({"deleted_at": "2026-07-14T00:00:00"}),
                    "created_at": "2026-07-14T00:00:00",
                },
            ]
            with patch.object(
                css, "_route_dirty_outbox_to_dlq",
                side_effect=mock_route_dlq,
            ):
                ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # 支持时应直接处理,返回 id
            assert ids == [1]
            # 不应调用 DLQ
            assert len(dlq_calls) == 0, "支持 soft_delete 时不应调用 DLQ"
            # 应执行 UPDATE
            assert mock_col.execute_raw.call_count == 1
            sql = mock_col.execute_raw.call_args[0][0]
            assert "UPDATE users SET" in sql
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 2. tombstone 不支持 → 进 DLQ,SQLite 写入成功 → dirty 标记 processed
# ════════════════════════════════════════════════════════════════

class TestTombstoneUnsupportedDlqSuccess:
    """R51 P0-9 场景 2: tombstone 不支持 → DLQ SQLite 写入成功 → dirty 标记 processed。"""

    @pytest.mark.asyncio
    async def test_tombstone_unsupported_dlq_sqlite_success(self, cache_store):
        """表不支持 soft_delete → DLQ SQLite 写入成功 → 返回所有 ids(标记 processed)。"""
        store = cache_store
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=False)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session

        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 10, "pk": "user-010", "operation": "tombstone",
                    "payload": None, "created_at": "2026-07-14T00:00:00",
                },
                {
                    "id": 11, "pk": "user-011", "operation": "tombstone",
                    "payload": None, "created_at": "2026-07-14T00:00:00",
                },
            ]
            # 不 mock _route_dirty_outbox_to_dlq,使用真实实现(写入 SQLite dlq_records)
            ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # DLQ SQLite 写入成功 → 返回所有 ids(标记 processed)
            assert ids == [10, 11], (
                f"DLQ 写入成功应返回所有 ids,实际: {ids}"
            )
            # 不应执行 UPDATE/DELETE
            assert mock_col.execute_raw.call_count == 0

            # 验证 dlq_records 表中有 2 条记录
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM dlq_records WHERE table_name = ?",
                ("users",),
            )
            row = await cursor.fetchone()
            assert row[0] == 2, f"dlq_records 应有 2 条记录,实际: {row[0]}"
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 3. tombstone 不支持 → 进 DLQ,SQLite 写入失败 → dirty 保持 pending
# ════════════════════════════════════════════════════════════════

class TestTombstoneUnsupportedDlqFailure:
    """R51 P0-9 场景 3: tombstone 不支持 → DLQ SQLite 写入失败 → dirty 保持 pending。"""

    @pytest.mark.asyncio
    async def test_tombstone_unsupported_dlq_sqlite_failure(self, cache_store):
        """表不支持 soft_delete → DLQ SQLite 写入失败 → 返回空列表(保持 pending)。"""
        store = cache_store
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=False)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session

        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 20, "pk": "user-020", "operation": "tombstone",
                    "payload": None, "created_at": "2026-07-14T00:00:00",
                },
            ]

            # mock insert_dlq_record 返回 0(写入失败)
            async def _failing_insert(*args, **kwargs):
                return 0
            with patch.object(
                store, "insert_dlq_record", side_effect=_failing_insert,
            ):
                ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # DLQ SQLite 写入失败 → 返回空列表(保持 dirty pending)
            assert ids == [], (
                f"DLQ 写入失败应返回空列表保持 pending,实际: {ids}"
            )
            # 不应执行 UPDATE/DELETE
            assert mock_col.execute_raw.call_count == 0
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)


# ════════════════════════════════════════════════════════════════
# 4. DLQ 写入失败 → 指数退避(next_retry_at 设置)
# ════════════════════════════════════════════════════════════════

class TestDlqFailureExponentialBackoff:
    """R51 P0-9 场景 4: DLQ 写入失败时设置 next_retry_at(指数退避)。"""

    @pytest.mark.asyncio
    async def test_dlq_failure_sets_next_retry_at(self, cache_store):
        """DLQ 写入失败 → mark_dirty_retry 设置 last_error + next_retry_at。"""
        store = cache_store

        # 向 dirty_outbox 插入测试记录
        ids = await _seed_dirty_outbox(store, "users", [
            {"id": 30, "pk": "user-030"},
        ])

        # 调用 mark_dirty_retry
        error_msg = "R51 P0-9: DLQ SQLite 写入失败,等待重试"
        updated = await store.mark_dirty_retry(ids, error_msg)
        assert updated == 1, f"应更新 1 行,实际: {updated}"

        # 验证 last_error 和 next_retry_at 已设置
        cursor = await store._db.execute(
            "SELECT last_error, next_retry_at FROM dirty_outbox WHERE id = ?",
            (ids[0],),
        )
        row = await cursor.fetchone()
        assert row is not None, "记录应存在"
        assert row[0] == error_msg, (
            f"last_error 应为 '{error_msg}',实际: {row[0]}"
        )
        assert row[1] is not None, "next_retry_at 应已设置(非 None)"
        assert "2026" in row[1], (
            f"next_retry_at 应为 ISO 时间戳,实际: {row[1]}"
        )

        # 验证 processed 仍为 0(保持 pending)
        cursor = await store._db.execute(
            "SELECT processed FROM dirty_outbox WHERE id = ?",
            (ids[0],),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "DLQ 失败时 processed 应保持 0(pending)"


# ════════════════════════════════════════════════════════════════
# 5. JSONL 写入成功但 SQLite 失败 → dirty 仍保持 pending
# ════════════════════════════════════════════════════════════════

class TestJsonlSuccessSqliteFailure:
    """R51 P0-9 场景 5: JSONL 写入成功但 SQLite 失败 → dirty 仍保持 pending。"""

    @pytest.mark.asyncio
    async def test_jsonl_success_sqlite_failure_keeps_pending(self, cache_store):
        """JSONL 镜像写入成功但 SQLite 权威写入失败 → success=False。"""
        store = cache_store
        mock_session, mock_col = _setup_mock_session_with_soft_delete(supports=False)

        original_session = sys.modules.get("database.session")
        sys.modules["database.session"] = mock_session

        try:
            import importlib
            import services.crdb_sync_service as css
            importlib.reload(css)
            css._reset_soft_delete_cache()

            records = [
                {
                    "id": 40, "pk": "user-040", "operation": "tombstone",
                    "payload": None, "created_at": "2026-07-14T00:00:00",
                },
            ]

            # mock insert_dlq_record 抛异常(SQLite 写入失败)
            async def _failing_insert(*args, **kwargs):
                raise RuntimeError("SQLite disk I/O error")
            with patch.object(
                store, "insert_dlq_record", side_effect=_failing_insert,
            ):
                ids = await css._dispatch_crdb_tombstone(records, "users", "user_id")

            # SQLite 写入失败 → 返回空列表(即使 JSONL 可能写成功)
            assert ids == [], (
                f"SQLite 失败时即使 JSONL 成功也应返回空列表,实际: {ids}"
            )

            # 验证 JSONL 文件可能已写入(镜像),但 dirty_outbox 不应标记 processed
            # (这里不验证 JSONL 文件内容,只验证 ids 返回为空)
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_route_dlq_returns_failure_when_sqlite_fails(self, cache_store):
        """_route_dirty_outbox_to_dlq SQLite 失败时返回 success=False。"""
        store = cache_store
        import services.crdb_sync_service as css

        records = [
            {
                "id": 41, "pk": "user-041", "operation": "tombstone",
                "payload": None, "created_at": "2026-07-14T00:00:00",
            },
        ]

        # mock insert_dlq_record 抛异常
        async def _failing_insert(*args, **kwargs):
            raise RuntimeError("SQLite disk I/O error")
        with patch.object(
            store, "insert_dlq_record", side_effect=_failing_insert,
        ):
            result = await css._route_dirty_outbox_to_dlq(
                "users", records, "test SQLite failure",
            )

        # SQLite 写入失败 → success=False
        assert result["success"] is False, (
            f"SQLite 失败时 success 应为 False,实际: {result['success']}"
        )
        assert 41 in result["failed_ids"], (
            f"failed_ids 应包含 41,实际: {result['failed_ids']}"
        )
        assert "SQLite" in result["error"] or "disk" in result["error"], (
            f"error 应包含 SQLite 错误信息,实际: {result['error']}"
        )


# ════════════════════════════════════════════════════════════════
# 6. 多个 dirty IDs 部分成功 → 只有成功的标记 processed
# ════════════════════════════════════════════════════════════════

class TestPartialSuccessDlqAck:
    """R51 P0-9 场景 6: 多个 dirty IDs 部分成功 → 只有成功的标记 processed。"""

    @pytest.mark.asyncio
    async def test_partial_success_only_marks_successful(self, cache_store):
        """部分 records 的 DLQ 写入成功,部分失败 → 只返回成功的 ids。"""
        store = cache_store
        import services.crdb_sync_service as css

        records = [
            {
                "id": 50, "pk": "user-050", "operation": "tombstone",
                "payload": None, "created_at": "2026-07-14T00:00:00",
            },
            {
                "id": 51, "pk": "user-051", "operation": "tombstone",
                "payload": None, "created_at": "2026-07-14T00:00:00",
            },
            {
                "id": 52, "pk": "user-052", "operation": "tombstone",
                "payload": None, "created_at": "2026-07-14T00:00:00",
            },
        ]

        # mock insert_dlq_record: id=50 成功, id=51 失败(返回 0), id=52 成功
        call_count = {"n": 0}
        async def _partial_insert(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # 第二条记录(id=51)写入失败
                return 0
            return call_count["n"]  # 返回非零 id 表示成功

        with patch.object(
            store, "insert_dlq_record", side_effect=_partial_insert,
        ):
            result = await css._route_dirty_outbox_to_dlq(
                "users", records, "test partial failure",
            )

        # 部分失败 → success=False(只要有任一记录失败)
        assert result["success"] is False, (
            f"部分失败时 success 应为 False,实际: {result['success']}"
        )
        # failed_ids 应包含 51(第二条记录)
        assert 51 in result["failed_ids"], (
            f"failed_ids 应包含 51,实际: {result['failed_ids']}"
        )
        # 50 和 52 不应在 failed_ids 中
        assert 50 not in result["failed_ids"], (
            f"50 应成功,不应在 failed_ids 中,实际: {result['failed_ids']}"
        )
        assert 52 not in result["failed_ids"], (
            f"52 应成功,不应在 failed_ids 中,实际: {result['failed_ids']}"
        )

    @pytest.mark.asyncio
    async def test_all_success_returns_empty_failed_ids(self, cache_store):
        """所有 records 的 DLQ 写入成功 → success=True, failed_ids 为空。"""
        store = cache_store
        import services.crdb_sync_service as css

        records = [
            {
                "id": 60, "pk": "user-060", "operation": "tombstone",
                "payload": None, "created_at": "2026-07-14T00:00:00",
            },
            {
                "id": 61, "pk": "user-061", "operation": "tombstone",
                "payload": None, "created_at": "2026-07-14T00:00:00",
            },
        ]

        # 不 mock insert_dlq_record,使用真实实现(全部成功)
        result = await css._route_dirty_outbox_to_dlq(
            "users", records, "test all success",
        )

        assert result["success"] is True, (
            f"全部成功时 success 应为 True,实际: {result['success']}"
        )
        assert result["failed_ids"] == [], (
            f"全部成功时 failed_ids 应为空,实际: {result['failed_ids']}"
        )
        assert result["error"] == "", (
            f"全部成功时 error 应为空字符串,实际: {result['error']}"
        )
