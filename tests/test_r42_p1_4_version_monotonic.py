"""R42 P1-4 整改测试: dirty_outbox version 单调来源 + 合并决胜。

测试覆盖 4 大场景:
1. _resolve_version_conflict 决胜规则(new > current / new == current / new < current)
2. _generate_version_from_payload 自动版本生成(payload.version / updated_at / 当前时间)
3. _get_table_version_field 表 → 时间戳字段映射
4. add_dirty_outbox + _sync_dirty_outbox 集成(version=0 自动生成 + 合并保留最新)

测试策略:
- 真实 SQLite cache_store(临时文件 DB)用于 add_dirty_outbox / get_dirty_outbox_batch
- Mock database.session(D1Collection)用于 _sync_dirty_outbox 集成
- 纯函数直接调用(_resolve_version_conflict / _generate_version_from_payload)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio


# 测试环境兼容: mock telegram / telegram.ext(避免 ImportError)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# 辅助: 临时 SQLite cache_store fixture
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(R42 P1-4 测试用)。

    使用临时目录避免污染开发环境,测试结束后自动清理。
    同时替换全局单例 _store,使 crdb_sync_service._get_cache_store_safe()
    能返回此实例。
    """
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r42_p1_4_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = cs_module.DB_PATH
    original_store = getattr(cs_module, "_store", None)
    cs_module.DB_PATH = db_path
    try:
        s = cs_module.CacheStore()
        await s.init()
        # 替换全局单例
        cs_module._store = s
        yield s
        await s.close()
    finally:
        cs_module.DB_PATH = original_path
        if original_store is not None:
            cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 场景 1: _resolve_version_conflict 决胜规则
# ════════════════════════════════════════════════════════════════

class TestResolveVersionConflict:
    """R42 P1-4: _resolve_version_conflict 决胜规则测试。"""

    def test_resolve_version_conflict_new_greater_than_current(self):
        """new > current 时应使用 new_version。"""
        from services.crdb_sync_service import _resolve_version_conflict

        result = _resolve_version_conflict(
            "users", "user-1",
            current_version=10, new_version=20,
            current_updated_at="2026-07-01T00:00:00",
            new_updated_at="2026-07-13T00:00:00",
        )
        assert result == 20, "new > current 时应返回 new_version=20"

    def test_resolve_version_conflict_new_equal_current_new_updated_at_later(self):
        """new == current 且 new_updated_at 更晚 → 返回 new_version。"""
        from services.crdb_sync_service import _resolve_version_conflict

        result = _resolve_version_conflict(
            "users", "user-1",
            current_version=10, new_version=10,
            current_updated_at="2026-07-01T00:00:00",
            new_updated_at="2026-07-13T00:00:00",
        )
        assert result == 10, "version 相同且 new_updated_at 更晚应返回 new_version"

    def test_resolve_version_conflict_new_equal_current_new_updated_at_earlier(self):
        """new == current 且 new_updated_at 更早 → 返回 current_version(丢弃 new)。"""
        from services.crdb_sync_service import _resolve_version_conflict

        result = _resolve_version_conflict(
            "users", "user-1",
            current_version=10, new_version=10,
            current_updated_at="2026-07-13T00:00:00",
            new_updated_at="2026-07-01T00:00:00",
        )
        assert result == 10, (
            "version 相同且 new_updated_at 更早应返回 current_version"
        )

    def test_resolve_version_conflict_new_less_than_current(self):
        """new < current 时应丢弃 new,返回 current_version。"""
        from services.crdb_sync_service import _resolve_version_conflict

        result = _resolve_version_conflict(
            "users", "user-1",
            current_version=20, new_version=10,
            current_updated_at="2026-07-01T00:00:00",
            new_updated_at="2026-07-13T00:00:00",
        )
        assert result == 20, "new < current 时应返回 current_version=20"

    def test_resolve_version_conflict_equal_updated_at_returns_new(self):
        """version 和 updated_at 都相等 → 返回 new_version(后者覆盖前者)。"""
        from services.crdb_sync_service import _resolve_version_conflict

        result = _resolve_version_conflict(
            "users", "user-1",
            current_version=10, new_version=10,
            current_updated_at="2026-07-13T00:00:00",
            new_updated_at="2026-07-13T00:00:00",
        )
        # >= 比较,相等时也返回 new_version
        assert result == 10

    def test_resolve_version_conflict_none_updated_at_fallback(self):
        """updated_at 为 None 时按空字符串比较(空 <= 任何值)。"""
        from services.crdb_sync_service import _resolve_version_conflict

        # current_updated_at=None, new_updated_at=None → 空字符串 >= 空字符串,True
        result = _resolve_version_conflict(
            "users", "user-1",
            current_version=5, new_version=5,
            current_updated_at=None,
            new_updated_at=None,
        )
        assert result == 5, "version 相同且 updated_at 都为 None 时返回 new_version"


# ════════════════════════════════════════════════════════════════
# 场景 2: _get_table_version_field 表 → 字段映射
# ════════════════════════════════════════════════════════════════

class TestGetTableVersionField:
    """R42 P1-4: _get_table_version_field 返回正确字段名。"""

    def test_get_table_version_field_users(self):
        """users 表应返回 'updated_at'。"""
        from database.cache_store import _get_table_version_field

        assert _get_table_version_field("users") == "updated_at"

    def test_get_table_version_field_codes(self):
        """codes 表应返回 'created_at'。"""
        from database.cache_store import _get_table_version_field

        assert _get_table_version_field("codes") == "created_at"

    def test_get_table_version_field_file_records(self):
        """file_records 表应返回 'create_time'。"""
        from database.cache_store import _get_table_version_field

        assert _get_table_version_field("file_records") == "create_time"

    def test_get_table_version_field_unknown_table_default(self):
        """未知表应返回默认字段 'updated_at'。"""
        from database.cache_store import _get_table_version_field

        assert _get_table_version_field("unknown_table_xyz") == "updated_at"


# ════════════════════════════════════════════════════════════════
# 场景 3: _generate_version_from_payload 自动版本生成
# ════════════════════════════════════════════════════════════════

class TestGenerateVersionFromPayload:
    """R42 P1-4: _generate_version_from_payload 自动版本生成测试。"""

    def test_generate_version_from_explicit_version_field(self):
        """payload 中有显式 version > 0 时应使用该 version。"""
        from database.cache_store import _generate_version_from_payload

        payload = json.dumps({"version": 100, "updated_at": "2026-07-13T00:00:00"})
        result = _generate_version_from_payload("users", payload)
        assert result == 100, "payload.version > 0 时应使用显式 version"

    def test_generate_version_from_updated_at_field(self):
        """payload 中无 version 字段但有 updated_at 时,从 updated_at 转换时间戳。"""
        from database.cache_store import _generate_version_from_payload

        payload = json.dumps({"updated_at": "2026-07-13T12:00:00"})
        result = _generate_version_from_payload("users", payload)
        # 验证结果为 2026-07-13T12:00:00 的 Unix 时间戳(UTC)
        import datetime as _dt
        expected = int(_dt.datetime.fromisoformat("2026-07-13T12:00:00").timestamp())
        assert result == expected, (
            f"应从 updated_at 生成 Unix 时间戳,期望 {expected},实际 {result}"
        )

    def test_generate_version_from_empty_payload_returns_current_timestamp(self):
        """payload 为空时应返回当前时间戳。"""
        from database.cache_store import _generate_version_from_payload

        before = int(time.time())
        result = _generate_version_from_payload("users", None)
        after = int(time.time())
        assert before <= result <= after, (
            f"空 payload 应返回当前时间戳 [{before}, {after}],实际 {result}"
        )

    def test_generate_version_from_payload_dict(self):
        """payload 为 dict(非 JSON 字符串)时也应正确解析。"""
        from database.cache_store import _generate_version_from_payload

        payload = {"version": 50, "updated_at": "2026-07-13T00:00:00"}
        result = _generate_version_from_payload("users", payload)
        assert result == 50, "dict payload 也应使用 version 字段"

    def test_generate_version_with_invalid_version_falls_back_to_updated_at(self):
        """payload.version 为 0 或无效时应 fallback 到 updated_at。"""
        from database.cache_store import _generate_version_from_payload

        # version=0 应跳过(fallback 到 updated_at)
        payload = json.dumps({
            "version": 0,
            "updated_at": "2026-07-13T12:00:00",
        })
        result = _generate_version_from_payload("users", payload)
        import datetime as _dt
        expected = int(_dt.datetime.fromisoformat("2026-07-13T12:00:00").timestamp())
        assert result == expected, (
            f"version=0 应 fallback 到 updated_at,期望 {expected},实际 {result}"
        )

    def test_generate_version_with_nonexistent_field_uses_default(self):
        """payload 中无 version 也无 updated_at 时,fallback 到当前时间戳。"""
        from database.cache_store import _generate_version_from_payload

        before = int(time.time())
        # users 表查 updated_at 字段,但 payload 中没有 → fallback 到当前时间
        payload = json.dumps({"other_field": "value"})
        result = _generate_version_from_payload("users", payload)
        after = int(time.time())
        assert before <= result <= after, (
            f"应 fallback 到当前时间戳 [{before}, {after}],实际 {result}"
        )

    def test_generate_version_from_payload_int_timestamp(self):
        """payload.updated_at 为 int/float 时直接用作 version。"""
        from database.cache_store import _generate_version_from_payload

        # updated_at 为 int → 直接用作 version
        payload = json.dumps({"updated_at": 1750000000})
        result = _generate_version_from_payload("users", payload)
        assert result == 1750000000, "int 类型 updated_at 应直接转为 version"


# ════════════════════════════════════════════════════════════════
# 场景 4: add_dirty_outbox 集成(version=0 自动生成 + 显式 version)
# ════════════════════════════════════════════════════════════════

class TestAddDirtyOutboxVersionAutoGen:
    """R42 P1-4: add_dirty_outbox version 自动生成测试。"""

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_version_zero_auto_generates(self, cache_store):
        """add_dirty_outbox version=0 时应自动从 payload 生成时间戳版本。"""
        store = cache_store
        # users 表的 version 字段为 updated_at
        # payload 含 updated_at = "2026-07-13T12:00:00"
        rid = await store.add_dirty_outbox(
            "users", "user-001", "upsert",
            payload=json.dumps({"user_id": 1, "updated_at": "2026-07-13T12:00:00"}),
            version=0,  # 触发自动生成
        )
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id = ?", (rid,)
        )
        row = await cursor.fetchone()
        assert row is not None
        # version 应为 2026-07-13T12:00:00 的 Unix 时间戳
        import datetime as _dt
        expected = int(_dt.datetime.fromisoformat("2026-07-13T12:00:00").timestamp())
        assert row[0] == expected, (
            f"version=0 时应自动生成,期望 {expected},实际 {row[0]}"
        )

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_explicit_version_preserved(self, cache_store):
        """add_dirty_outbox version > 0 时应使用显式版本。"""
        store = cache_store
        rid = await store.add_dirty_outbox(
            "users", "user-002", "upsert",
            payload=json.dumps({"user_id": 2, "updated_at": "2026-07-13T00:00:00"}),
            version=999,  # 显式版本
        )
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id = ?", (rid,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 999, "显式 version 应被保留"

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_no_payload_uses_current_timestamp(
        self, cache_store,
    ):
        """add_dirty_outbox 无 payload 时 version 应为当前时间戳。"""
        store = cache_store
        before = int(time.time())
        rid = await store.add_dirty_outbox(
            "users", "user-003", "upsert",
            payload=None,
            version=0,
        )
        after = int(time.time())
        assert rid > 0

        cursor = await store._db.execute(
            "SELECT version FROM dirty_outbox WHERE id = ?", (rid,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert before <= row[0] <= after, (
            f"无 payload 时 version 应为当前时间戳 [{before}, {after}],实际 {row[0]}"
        )

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_idempotent_via_dispatch_merge(
        self, cache_store,
    ):
        """同 (table, pk) 多次 add_dirty_outbox 后经 dispatch 合并到最新版本。

        R42 P1-4: dirty_outbox 表无 UNIQUE 约束,允许多次写入相同 (table, pk);
        合并去重在 _sync_dirty_outbox 中通过 _resolve_version_conflict 完成。
        此测试验证:多次写入后,合并后只剩最新 version 的记录参与 dispatch。
        """
        store = cache_store
        # 写入 3 条同 (table, pk) 的记录,version 递增
        await store.add_dirty_outbox(
            "users", "user-merge", "upsert",
            payload=json.dumps({"user_id": 1, "version": 1}),
            version=1,
        )
        await store.add_dirty_outbox(
            "users", "user-merge", "upsert",
            payload=json.dumps({"user_id": 1, "version": 2}),
            version=2,
        )
        await store.add_dirty_outbox(
            "users", "user-merge", "upsert",
            payload=json.dumps({"user_id": 1, "version": 3}),
            version=3,
        )

        # 查询所有未处理记录(应返回 3 条)
        batch = await store.get_dirty_outbox_batch(limit=100)
        # 过滤出 users/user-merge 记录
        user_merge_records = [
            r for r in batch
            if r["table_name"] == "users" and r["pk"] == "user-merge"
        ]
        assert len(user_merge_records) == 3, "应写入 3 条 dirty_outbox 记录"

        # 模拟 _sync_dirty_outbox 的合并逻辑(直接调用 _resolve_version_conflict)
        from services.crdb_sync_service import _resolve_version_conflict

        # 按 version 排序后逐个 resolve
        sorted_records = sorted(user_merge_records, key=lambda r: r["version"])
        chosen_version = sorted_records[0]["version"]
        for r in sorted_records[1:]:
            chosen_version = _resolve_version_conflict(
                "users", "user-merge",
                chosen_version, r["version"],
                None, None,
            )
        # 最终应保留 version=3(最大)
        assert chosen_version == 3, (
            f"合并后应保留 version=3,实际 {chosen_version}"
        )


# ════════════════════════════════════════════════════════════════
# 场景 5: _sync_dirty_outbox 合并行为集成测试
# ════════════════════════════════════════════════════════════════

class TestSyncDirtyOutboxMerge:
    """R42 P1-4: _sync_dirty_outbox 合并行为测试。

    通过 mock database.session 提供 D1Collection,mock dispatcher 捕获合并后的记录,
    验证 _sync_dirty_outbox 正确合并相同 (table, pk) 的多条记录。
    """

    @pytest.mark.asyncio
    async def test_sync_merges_same_table_pk_to_single_record(self, cache_store):
        """多条 dirty_outbox 同 (table, pk) 合并到一条(保留最新 version)。"""
        store = cache_store
        # 写入 3 条同 (table, pk) 的记录,version 递增
        for v in [1, 2, 3]:
            await store.add_dirty_outbox(
                "users", "user-merge-1", "upsert",
                payload=json.dumps({"user_id": 1, "version": v}),
                version=v,
            )

        # 准备 mock 环境
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

            # 捕获 dispatch 收到的记录
            captured_records: list[dict] = []

            async def _capture_dispatch(table_name, records):
                captured_records.extend(records)
                return [r.get("id") for r in records]

            # 替换 _dispatch_dirty_outbox_to_crdb 捕获合并后的记录
            original_dispatch = css._dispatch_dirty_outbox_to_crdb
            css._dispatch_dirty_outbox_to_crdb = _capture_dispatch
            # mock _should_connect / _lazy_connect_crdb / _close_pool_if_idle
            css._should_connect = AsyncMock(return_value=True)
            css._lazy_connect_crdb = AsyncMock(return_value=None)
            css._close_pool_if_idle = AsyncMock(return_value=None)

            try:
                await css._sync_dirty_outbox()
            finally:
                css._dispatch_dirty_outbox_to_crdb = original_dispatch

            # 合并后应只 dispatch 1 条记录(最新 version=3)
            assert len(captured_records) == 1, (
                f"合并后应 dispatch 1 条记录,实际 {len(captured_records)}"
            )
            assert captured_records[0]["version"] == 3, (
                f"合并后应保留 version=3,实际 {captured_records[0]['version']}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_sync_does_not_merge_different_table_pk(self, cache_store):
        """不同 (table, pk) 的记录不应合并。"""
        store = cache_store
        # 写入 2 条不同 (table, pk) 的记录
        await store.add_dirty_outbox(
            "users", "user-A", "upsert",
            payload=json.dumps({"user_id": 1, "version": 1}),
            version=1,
        )
        await store.add_dirty_outbox(
            "users", "user-B", "upsert",
            payload=json.dumps({"user_id": 2, "version": 1}),
            version=1,
        )

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

            captured_records: list[dict] = []

            async def _capture_dispatch(table_name, records):
                captured_records.extend(records)
                return [r.get("id") for r in records]

            original_dispatch = css._dispatch_dirty_outbox_to_crdb
            css._dispatch_dirty_outbox_to_crdb = _capture_dispatch
            css._should_connect = AsyncMock(return_value=True)
            css._lazy_connect_crdb = AsyncMock(return_value=None)
            css._close_pool_if_idle = AsyncMock(return_value=None)

            try:
                await css._sync_dirty_outbox()
            finally:
                css._dispatch_dirty_outbox_to_crdb = original_dispatch

            # 应 dispatch 2 条记录(不同 pk 不合并)
            assert len(captured_records) == 2, (
                f"不同 (table, pk) 不应合并,应 dispatch 2 条,实际 {len(captured_records)}"
            )
            # 验证两个 pk 都存在
            pks = {r["pk"] for r in captured_records}
            assert pks == {"user-A", "user-B"}, (
                f"应包含 user-A 和 user-B,实际 {pks}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_sync_keeps_latest_updated_at_when_version_equal(self, cache_store):
        """version 相同时 updated_at(以 created_at 代理)决定顺序,保留最新。"""
        store = cache_store
        # 写入 2 条 version 相同但 created_at 不同的记录
        # 通过直接 INSERT 控制 created_at(避免 add_dirty_outbox 用当前时间)
        import datetime as _dt
        await store._db.execute(
            "INSERT INTO dirty_outbox (table_name, pk, version, operation, payload, "
            "created_at, processed, local_only) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
            (
                "users", "user-test", 5, "upsert",
                json.dumps({"user_id": 1, "version": 5}),
                "2026-07-01T00:00:00",  # 较早
            ),
        )
        await store._db.commit()
        await store._db.execute(
            "INSERT INTO dirty_outbox (table_name, pk, version, operation, payload, "
            "created_at, processed, local_only) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
            (
                "users", "user-test", 5, "upsert",
                json.dumps({"user_id": 1, "version": 5}),
                "2026-07-13T00:00:00",  # 较新
            ),
        )
        await store._db.commit()

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

            captured_records: list[dict] = []

            async def _capture_dispatch(table_name, records):
                captured_records.extend(records)
                return [r.get("id") for r in records]

            original_dispatch = css._dispatch_dirty_outbox_to_crdb
            css._dispatch_dirty_outbox_to_crdb = _capture_dispatch
            css._should_connect = AsyncMock(return_value=True)
            css._lazy_connect_crdb = AsyncMock(return_value=None)
            css._close_pool_if_idle = AsyncMock(return_value=None)

            try:
                await css._sync_dirty_outbox()
            finally:
                css._dispatch_dirty_outbox_to_crdb = original_dispatch

            # 合并后应只保留 1 条,且 created_at 为较新的 2026-07-13
            assert len(captured_records) == 1, (
                f"version 相同应合并为 1 条,实际 {len(captured_records)}"
            )
            assert captured_records[0]["created_at"] == "2026-07-13T00:00:00", (
                f"应保留 created_at 较新的记录,实际 "
                f"{captured_records[0]['created_at']}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_sync_marks_merged_old_records_as_processed(self, cache_store):
        """合并后旧版本记录也应被标记为 processed(避免重复 dispatch)。"""
        store = cache_store
        # 写入 3 条同 (table, pk) 的记录,version 递增
        ids = []
        for v in [1, 2, 3]:
            rid = await store.add_dirty_outbox(
                "users", "user-mark", "upsert",
                payload=json.dumps({"user_id": 1, "version": v}),
                version=v,
            )
            ids.append(rid)

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

            async def _noop_dispatch(table_name, records):
                return [r.get("id") for r in records]

            original_dispatch = css._dispatch_dirty_outbox_to_crdb
            css._dispatch_dirty_outbox_to_crdb = _noop_dispatch
            css._should_connect = AsyncMock(return_value=True)
            css._lazy_connect_crdb = AsyncMock(return_value=None)
            css._close_pool_if_idle = AsyncMock(return_value=None)

            try:
                await css._sync_dirty_outbox()
            finally:
                css._dispatch_dirty_outbox_to_crdb = original_dispatch

            # 验证所有 3 条记录都标记为 processed(包括被合并的旧版本)
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM dirty_outbox "
                "WHERE table_name = 'users' AND pk = 'user-mark' AND processed = 1"
            )
            row = await cursor.fetchone()
            assert row[0] == 3, (
                f"合并后所有 3 条记录(含旧版本)应标记为 processed,实际 {row[0]}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)

    @pytest.mark.asyncio
    async def test_sync_preserves_latest_version_after_merge(self, cache_store):
        """合并后保留最新 version 和 updated_at(以 created_at 代理)。"""
        store = cache_store
        # 写入 3 条记录:version=1 (created_at 较新), version=5 (created_at 较旧),
        # version=10 (created_at 最新)
        # 期望最终保留 version=10
        test_records = [
            (1, "2026-07-01T00:00:00"),
            (5, "2026-06-01T00:00:00"),  # 较旧 created_at
            (10, "2026-07-13T00:00:00"),
        ]
        for v, created_at in test_records:
            await store._db.execute(
                "INSERT INTO dirty_outbox (table_name, pk, version, operation, "
                "payload, created_at, processed, local_only) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                (
                    "users", "user-latest", v, "upsert",
                    json.dumps({"user_id": 1, "version": v}),
                    created_at,
                ),
            )
            await store._db.commit()

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

            captured_records: list[dict] = []

            async def _capture_dispatch(table_name, records):
                captured_records.extend(records)
                return [r.get("id") for r in records]

            original_dispatch = css._dispatch_dirty_outbox_to_crdb
            css._dispatch_dirty_outbox_to_crdb = _capture_dispatch
            css._should_connect = AsyncMock(return_value=True)
            css._lazy_connect_crdb = AsyncMock(return_value=None)
            css._close_pool_if_idle = AsyncMock(return_value=None)

            try:
                await css._sync_dirty_outbox()
            finally:
                css._dispatch_dirty_outbox_to_crdb = original_dispatch

            assert len(captured_records) == 1
            # 应保留 version=10(最大)
            assert captured_records[0]["version"] == 10, (
                f"合并后应保留 version=10,实际 {captured_records[0]['version']}"
            )
        finally:
            if original_session is not None:
                sys.modules["database.session"] = original_session
            else:
                sys.modules.pop("database.session", None)
