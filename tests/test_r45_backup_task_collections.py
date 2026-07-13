"""R45 第 15-16 节: Backup/TaskCenter/Collections/Notifications/Maintenance/Repair 测试。

被测目标(R45 整改):
- services/backup_engine.py: _validate_cross_table_invariants 跨表不变量验证
- services/task_center.py: get_user_task/update_task_progress/list_user_tasks(cursor)/list_all_tasks(filters)
- services/collections.py: update_collection(CAS 乐观锁)/batch_retrieve/list_collections(cursor)
- services/notifications.py: notification_outbox + record_notification_receipt + dedup_key
- services/maintenance_mode.py: check_maintenance_at_entry(统一入口检查 fail-closed)
- services/repair_console.py: SAFE_ACTIONS/execute_repair/get_causal_chain/compute_payload_hash

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db),
  通过设置 _cs_module._store 让所有模块的 get_cache_store() 返回测试 store。
- 通过直接 INSERT 插入测试数据(用户 / 文件记录 / 集合 + 集合项)。
- 通过 mock redis_queue 模块避免依赖真实 Redis。
"""
import inspect
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock telegram 模块(python-telegram-bot 未安装于测试环境)
# collections.py → code_generator → file_utils → telegram
# 在导入 services.collections 之前注入,避免 ModuleNotFoundError
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())
# Mock redis_queue 模块(避免依赖真实 Redis)
_redis_queue_mock = MagicMock()
_redis_queue_mock.get_dead_messages = AsyncMock(return_value=[])
_redis_queue_mock.get_dlq_length = AsyncMock(return_value=0)
_redis_queue_mock.requeue_from_dlq = AsyncMock(return_value=False)
sys.modules.setdefault("database.redis_queue", _redis_queue_mock)
# Mock relay_db
_relay_db_mock = MagicMock()
_relay_db_inst = MagicMock()
_relay_db_inst._db = MagicMock()
_relay_db_inst._db.execute_fetchall = AsyncMock(return_value=[])
_relay_db_inst._db.execute = AsyncMock()
_relay_db_inst._db.commit = AsyncMock()
_relay_db_mock.get_relay_db = AsyncMock(return_value=_relay_db_inst)
sys.modules.setdefault("database.relay_db", _relay_db_mock)

import pytest
import pytest_asyncio

from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(共享给所有 service 模块)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def store():
    """创建使用临时文件数据库的 CacheStore 实例。

    设置 _cs_module._store 让所有模块(task_center / notifications / collections /
    maintenance_mode / repair_console)的 get_cache_store() 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r45_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s  # 让 get_cache_store() 返回测试 store
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def reset_maintenance_cache():
    """每个用例前重置 maintenance_mode 模块级缓存。"""
    from services import maintenance_mode
    maintenance_mode._reset_cache_for_test()
    yield
    maintenance_mode._reset_cache_for_test()


@pytest_asyncio.fixture
async def reset_outbox_schema():
    """每个用例前重置 notifications outbox schema 标记。"""
    from services import notifications
    notifications._reset_outbox_schema_for_test()
    yield
    notifications._reset_outbox_schema_for_test()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_file_record(store, file_code: str, uploader_id: int = 0,
                               status: str = "active", deleted: bool = False,
                               expire_time=None):
    """插入测试文件记录到 file_records_local。"""
    now = datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO file_records_local
           (file_code, uploader_id, status, deleted_at, expire_time, crdb_synced)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (file_code, uploader_id,
         "deleted" if deleted else status,
         now if deleted else None,
         expire_time,
         ),
    )
    await store._db.commit()


async def _insert_command_execution(store, action_id: str, status: str = "executed",
                                     request_hash: str = "test_hash"):
    """插入 command_executions 记录。"""
    now = datetime.now().isoformat()
    await store._db.execute(
        """INSERT INTO command_executions
           (action_id, command_type, principal_id, status, owner,
            lease_until, request_hash, result, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
        (action_id, "test_command", 100, status, "test_worker",
         request_hash, '{"success": true}', now, now),
    )
    await store._db.commit()


# ════════════════════════════════════════════════════════════════
# 1. TaskCenter 用户隔离测试
# ════════════════════════════════════════════════════════════════

class TestTaskCenterUserIsolation:
    """R45 第 16 节: TaskCenter 用户查询隔离。"""

    @pytest.mark.asyncio
    async def test_get_user_task_owner_allowed(self, store):
        """用户查询自己的任务 → 返回任务详情。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1001, task_type="upload", status="pending",
            metadata={"file_code": "AAA"},
        )
        assert task_id > 0
        result = await task_center.get_user_task(task_id, user_id=1001)
        assert result is not None
        assert result["id"] == task_id
        assert result["user_id"] == 1001

    @pytest.mark.asyncio
    async def test_get_user_task_cross_user_denied(self, store):
        """用户 A 查询用户 B 的任务 → 返回 None(跨用户拒绝)。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1001, task_type="upload", status="pending",
            metadata={"file_code": "BBB"},
        )
        assert task_id > 0
        # 用户 1002 试图查询用户 1001 的任务
        result = await task_center.get_user_task(task_id, user_id=1002)
        assert result is None, "跨用户查询应被拒绝(返回 None)"

    @pytest.mark.asyncio
    async def test_get_user_task_not_found(self, store):
        """查询不存在的任务 → 返回 None。"""
        from services import task_center
        result = await task_center.get_user_task(task_id=99999, user_id=1001)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_task_progress(self, store):
        """update_task_progress 更新进度和 ETA。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=2001, task_type="index", status="running",
            metadata={"file_code": "CCC"},
        )
        ok = await task_center.update_task_progress(task_id, progress=50, eta_seconds=120)
        assert ok is True
        task = await task_center.get_task(task_id)
        assert task["progress"] == 50
        assert task["eta_seconds"] == 120

    @pytest.mark.asyncio
    async def test_update_task_progress_clamped(self, store):
        """progress 超出 [0,100] 范围时被截断。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=2002, task_type="copy", status="running",
            metadata={},
        )
        # 超出上限
        await task_center.update_task_progress(task_id, progress=150, eta_seconds=0)
        task = await task_center.get_task(task_id)
        assert task["progress"] == 100
        # 低于下限
        await task_center.update_task_progress(task_id, progress=-10, eta_seconds=0)
        task = await task_center.get_task(task_id)
        assert task["progress"] == 0


# ════════════════════════════════════════════════════════════════
# 2. TaskCenter cursor 分页测试
# ════════════════════════════════════════════════════════════════

class TestTaskCenterCursorPagination:
    """R45 第 16 节: list_user_tasks cursor 分页。"""

    @pytest.mark.asyncio
    async def test_list_user_tasks_first_page(self, store):
        """首页返回最新 limit 条 + has_more 标志。"""
        from services import task_center
        # 创建 5 个任务
        for i in range(5):
            await task_center.record_task(
                user_id=3001, task_type="upload", status="completed",
                metadata={"index": i},
            )
        # 首页 page_size=3
        result = await task_center.list_user_tasks(
            user_id=3001, status="completed", limit=3, cursor=0,
        )
        assert isinstance(result, dict)
        assert "items" in result
        assert "next_cursor" in result
        assert "has_more" in result
        assert len(result["items"]) == 3
        assert result["has_more"] is True
        assert result["next_cursor"] > 0

    @pytest.mark.asyncio
    async def test_list_user_tasks_second_page(self, store):
        """第二页使用 next_cursor 获取剩余数据。"""
        from services import task_center
        for i in range(5):
            await task_center.record_task(
                user_id=3002, task_type="upload", status="completed",
                metadata={"index": i},
            )
        # 第一页
        page1 = await task_center.list_user_tasks(
            user_id=3002, status="completed", limit=3, cursor=0,
        )
        assert page1["has_more"] is True
        next_cursor = page1["next_cursor"]
        # 第二页
        page2 = await task_center.list_user_tasks(
            user_id=3002, status="completed", limit=3, cursor=next_cursor,
        )
        assert len(page2["items"]) == 2  # 剩余 2 个
        assert page2["has_more"] is False
        assert page2["next_cursor"] == 0

    @pytest.mark.asyncio
    async def test_list_all_tasks_with_filters(self, store):
        """list_all_tasks 支持 filters (user_id/task_type/trace_id),返回 list[dict]。"""
        from services import task_center
        # record_task 不接受 trace_id 参数,先创建任务再通过 SQL 设置 trace_id
        t1 = await task_center.record_task(
            user_id=4001, task_type="upload", status="completed",
            metadata={"k": "v1"},
        )
        t2 = await task_center.record_task(
            user_id=4002, task_type="index", status="pending",
            metadata={"k": "v2"},
        )
        # 通过 SQL 直接设置 trace_id(record_task 不接受 trace_id 参数)
        await store._db.execute(
            "UPDATE tasks SET trace_id = ? WHERE id = ?",
            ("trace_A", t1),
        )
        await store._db.execute(
            "UPDATE tasks SET trace_id = ? WHERE id = ?",
            ("trace_B", t2),
        )
        await store._db.commit()
        # 按 user_id 过滤
        result = await task_center.list_all_tasks(
            filters={"user_id": 4001}
        )
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(it["user_id"] == 4001 for it in result)

        # 按 task_type 过滤
        result = await task_center.list_all_tasks(
            filters={"task_type": "index"}
        )
        assert all(it["task_type"] == "index" for it in result)

        # 按 trace_id 过滤
        result = await task_center.list_all_tasks(
            filters={"trace_id": "trace_B"}
        )
        assert all(it.get("trace_id") == "trace_B" for it in result)


# ════════════════════════════════════════════════════════════════
# 3. Collections 乐观锁(CAS)测试
# ════════════════════════════════════════════════════════════════

class TestCollectionsOptimisticLock:
    """R45 第 16 节: Collections CAS 乐观锁。"""

    @pytest.mark.asyncio
    async def test_update_collection_with_valid_version(self, store):
        """使用正确的 expected_version → 更新成功 + 版本号递增。"""
        from services import collections
        coll = await collections.create_collection("测试集合", owner_id=5001)
        coll_id = coll["id"]
        # 读取初始版本
        initial_version = coll.get("version", 1) if "version" in coll else 1
        # 通过 get_collection 查询实际版本号
        coll_info = await collections.get_collection(coll["code"])
        actual_version = coll_info["version"] if coll_info else 1
        # CAS 更新
        result = await collections.update_collection(
            collection_id=coll_id,
            name="更新后名称",
            expected_version=actual_version,
        )
        assert result["success"] is True
        assert result["conflict"] is False
        assert result["new_version"] == actual_version + 1

    @pytest.mark.asyncio
    async def test_update_collection_stale_version_conflict(self, store):
        """使用过期的 expected_version → 冲突,不更新。"""
        from services import collections
        coll = await collections.create_collection("冲突测试", owner_id=5002)
        coll_id = coll["id"]
        coll_info = await collections.get_collection(coll["code"])
        actual_version = coll_info["version"]
        # 第一次更新成功 → 版本号 +1
        await collections.update_collection(
            collection_id=coll_id, name="第一次更新",
            expected_version=actual_version,
        )
        # 第二次使用旧版本号 → 冲突
        result = await collections.update_collection(
            collection_id=coll_id, name="第二次更新(冲突)",
            expected_version=actual_version,  # 旧版本号
        )
        assert result["success"] is False
        assert result["conflict"] is True
        assert "版本冲突" in result["message"]
        # 当前版本应该是 actual_version + 1(第一次更新后)
        assert result["current_version"] == actual_version + 1

    @pytest.mark.asyncio
    async def test_update_collection_without_expected_version(self, store):
        """不传 expected_version → 跳过乐观锁,直接更新(向后兼容)。"""
        from services import collections
        coll = await collections.create_collection("兼容模式", owner_id=5003)
        coll_id = coll["id"]
        result = await collections.update_collection(
            collection_id=coll_id, description="新描述",
            expected_version=None,
        )
        assert result["success"] is True
        assert result["conflict"] is False

    @pytest.mark.asyncio
    async def test_update_collection_not_exists(self, store):
        """更新不存在的集合 → success=False, conflict=False。"""
        from services import collections
        result = await collections.update_collection(
            collection_id=99999, name="不存在",
            expected_version=1,
        )
        assert result["success"] is False
        assert result["conflict"] is False
        assert "不存在" in result["message"]

    @pytest.mark.asyncio
    async def test_update_collection_no_fields(self, store):
        """无字段需更新 → success=True,版本不变。"""
        from services import collections
        coll = await collections.create_collection("空更新", owner_id=5004)
        coll_id = coll["id"]
        coll_info = await collections.get_collection(coll["code"])
        actual_version = coll_info["version"]
        result = await collections.update_collection(
            collection_id=coll_id,
            expected_version=actual_version,
        )
        assert result["success"] is True
        assert result["new_version"] == actual_version


# ════════════════════════════════════════════════════════════════
# 4. Collections batch_retrieve 测试
# ════════════════════════════════════════════════════════════════

class TestCollectionsBatchRetrieve:
    """R45 第 16 节: batch_retrieve 逐项校验。"""

    @pytest.mark.asyncio
    async def test_batch_retrieve_owner_all_retrievable(self, store):
        """owner 取件 → 所有项 retrievable。"""
        from services import collections
        coll = await collections.create_collection("owner合集", owner_id=6001)
        coll_id = coll["id"]
        await _insert_file_record(store, "FILE_R001", uploader_id=6001)
        await _insert_file_record(store, "FILE_R002", uploader_id=6001)
        await collections.add_files(coll_id, ["FILE_R001", "FILE_R002"])
        result = await collections.batch_retrieve(coll_id, user_id=6001)
        assert result["error"] == ""
        assert result["is_owner"] is True
        assert result["total_count"] == 2
        assert result["retrievable_count"] == 2
        assert result["all_retrievable"] is True
        assert result["denied_count"] == 0
        assert all(it["retrieval_status"] == "retrievable" for it in result["items"])

    @pytest.mark.asyncio
    async def test_batch_retrieve_partial_denied(self, store):
        """非 owner + 部分 uploader 匹配 → 部分允许,部分拒绝。"""
        from services import collections
        coll = await collections.create_collection("混合合集", owner_id=6002)
        coll_id = coll["id"]
        # FILE_P001 的 uploader 是 6003(匹配)
        # FILE_P002 的 uploader 是 9999(不匹配)
        await _insert_file_record(store, "FILE_P001", uploader_id=6003)
        await _insert_file_record(store, "FILE_P002", uploader_id=9999)
        await collections.add_files(coll_id, ["FILE_P001", "FILE_P002"])
        result = await collections.batch_retrieve(coll_id, user_id=6003)
        assert result["is_owner"] is False
        assert result["retrievable_count"] == 1
        assert result["denied_count"] == 1
        assert result["all_retrievable"] is False
        # 验证具体项的状态
        item_001 = next(it for it in result["items"] if it["file_code"] == "FILE_P001")
        assert item_001["retrieval_status"] == "retrievable"
        item_002 = next(it for it in result["items"] if it["file_code"] == "FILE_P002")
        assert item_002["retrieval_status"] == "denied"

    @pytest.mark.asyncio
    async def test_batch_retrieve_expired_file(self, store):
        """包含过期文件 → 标记为 expired,不阻塞其他项。"""
        from services import collections
        coll = await collections.create_collection("过期合集", owner_id=6004)
        coll_id = coll["id"]
        await _insert_file_record(store, "FILE_E001", uploader_id=6004, status="active")
        # FILE_E002 已过期
        await _insert_file_record(store, "FILE_E002", uploader_id=6004, status="expired")
        await collections.add_files(coll_id, ["FILE_E001", "FILE_E002"])
        result = await collections.batch_retrieve(coll_id, user_id=6004)
        assert result["retrievable_count"] == 1
        assert result["expired_count"] == 1
        assert result["all_retrievable"] is False
        item_e002 = next(it for it in result["items"] if it["file_code"] == "FILE_E002")
        assert item_e002["retrieval_status"] == "expired"

    @pytest.mark.asyncio
    async def test_batch_retrieve_deleted_file(self, store):
        """包含已删除文件 → 标记为 deleted。"""
        from services import collections
        coll = await collections.create_collection("删除合集", owner_id=6005)
        coll_id = coll["id"]
        await _insert_file_record(store, "FILE_D001", uploader_id=6005)
        await _insert_file_record(store, "FILE_D002", uploader_id=6005, deleted=True)
        await collections.add_files(coll_id, ["FILE_D001", "FILE_D002"])
        result = await collections.batch_retrieve(coll_id, user_id=6005)
        assert result["retrievable_count"] == 1
        assert result["deleted_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_retrieve_collection_disabled(self, store):
        """集合已禁用 → 全部不可取件,error 提示。"""
        from services import collections
        coll = await collections.create_collection("禁用合集", owner_id=6006)
        coll_id = coll["id"]
        await _insert_file_record(store, "FILE_X001", uploader_id=6006)
        await collections.add_files(coll_id, ["FILE_X001"])
        # 手动标记集合为 disabled
        await store._db.execute(
            "UPDATE collections SET status = 'disabled' WHERE id = ?",
            (coll_id,),
        )
        await store._db.commit()
        result = await collections.batch_retrieve(coll_id, user_id=6006)
        assert result["total_count"] == 0
        assert "已禁用" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_retrieve_not_exists(self, store):
        """不存在的集合 → error 提示。"""
        from services import collections
        result = await collections.batch_retrieve(99999, user_id=6007)
        assert result["total_count"] == 0
        assert "不存在" in result["error"]


# ════════════════════════════════════════════════════════════════
# 5. Collections cursor 分页测试
# ════════════════════════════════════════════════════════════════

class TestCollectionsCursorPagination:
    """R45 第 16 节: list_collections cursor 分页。"""

    @pytest.mark.asyncio
    async def test_list_collections_first_page(self, store):
        """首页返回最新 limit 条 + has_more 标志。"""
        from services import collections
        for i in range(5):
            await collections.create_collection(f"集合_{i}", owner_id=7001)
        result = await collections.list_collections(
            owner_id=7001, page=1, page_size=3, cursor=0,
        )
        assert "next_cursor" in result
        assert "has_more" in result
        assert len(result["items"]) == 3
        assert result["has_more"] is True
        assert result["next_cursor"] > 0

    @pytest.mark.asyncio
    async def test_list_collections_second_page(self, store):
        """使用 next_cursor 获取下一页。"""
        from services import collections
        for i in range(5):
            await collections.create_collection(f"集合2_{i}", owner_id=7002)
        page1 = await collections.list_collections(
            owner_id=7002, page=1, page_size=3, cursor=0,
        )
        assert page1["has_more"] is True
        page2 = await collections.list_collections(
            owner_id=7002, page=1, page_size=3, cursor=page1["next_cursor"],
        )
        assert len(page2["items"]) == 2
        assert page2["has_more"] is False
        assert page2["next_cursor"] == 0

    @pytest.mark.asyncio
    async def test_list_collections_backward_compat(self, store):
        """不传 cursor(向后兼容) → 返回 page/page_size/total_pages 字段。"""
        from services import collections
        for i in range(3):
            await collections.create_collection(f"兼容_{i}", owner_id=7003)
        # 调用时不传 cursor(默认 0,但 page=1 触发 page 模式)
        result = await collections.list_collections(
            owner_id=7003, page=1, page_size=10,  # cursor 默认 0
        )
        # 向后兼容字段存在
        assert "total" in result
        assert "page" in result
        assert "page_size" in result
        assert "total_pages" in result
        assert result["total"] == 3


# ════════════════════════════════════════════════════════════════
# 6. Notifications outbox 测试
# ════════════════════════════════════════════════════════════════

class TestNotificationsOutbox:
    """R45 第 16 节: notification_outbox + delivery_receipt。"""

    @pytest.mark.asyncio
    async def test_send_writes_to_outbox(self, store, reset_outbox_schema):
        """send() 同事务写入 notification_outbox(pending)。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=8001, notif_type="ready",
            payload={"file_code": "OUT001"},
        )
        assert notif_id > 0
        # 验证 notification_outbox 有记录
        rows = await store._db.execute_fetchall(
            "SELECT id, notif_id, status, dedup_key FROM notification_outbox "
            "WHERE notif_id = ?",
            (notif_id,),
        )
        assert len(rows) == 1
        assert rows[0][2] == "pending"  # status
        assert rows[0][3] == ""  # dedup_key 空(无 dedup_key 时)

    @pytest.mark.asyncio
    async def test_send_with_dedup_key_in_payload(self, store, reset_outbox_schema):
        """payload 中包含 _dedup_key 时,outbox 记录 dedup_key。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=8002, notif_type="ready",
            payload={"file_code": "OUT002", "_dedup_key": "task_complete:99"},
        )
        assert notif_id > 0
        rows = await store._db.execute_fetchall(
            "SELECT dedup_key FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "task_complete:99"

    @pytest.mark.asyncio
    async def test_record_receipt_delivered(self, store, reset_outbox_schema):
        """投递成功 → outbox.status='delivered' + notification_receipts 有记录。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=8003, notif_type="ready",
            payload={"file_code": "OUT003"},
        )
        receipt_id = await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=8003,
            channel="telegram", status="delivered",
        )
        assert receipt_id > 0
        # outbox 状态应为 delivered
        rows = await store._db.execute_fetchall(
            "SELECT status, delivered_at FROM notification_outbox "
            "WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "delivered"
        assert rows[0][1] is not None  # delivered_at 非空
        # notification_receipts 表应有记录
        receipts = await store._db.execute_fetchall(
            "SELECT status FROM notification_receipts WHERE notif_id = ?",
            (notif_id,),
        )
        assert len(receipts) == 1
        assert receipts[0][0] == "delivered"

    @pytest.mark.asyncio
    async def test_record_receipt_failed(self, store, reset_outbox_schema):
        """投递失败 → outbox.status='failed' + attempts + 1。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=8004, notif_type="ready",
            payload={"file_code": "OUT004"},
        )
        receipt_id = await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=8004,
            channel="telegram", status="failed",
            error="user_blocked_bot",
        )
        assert receipt_id > 0
        rows = await store._db.execute_fetchall(
            "SELECT status, attempts, last_error FROM notification_outbox "
            "WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "failed"  # status
        assert rows[0][1] == 1  # attempts
        assert "user_blocked_bot" in rows[0][2]  # last_error

    @pytest.mark.asyncio
    async def test_record_receipt_failed_max_attempts_skipped(
        self, store, reset_outbox_schema
    ):
        """达到 max_attempts 时 → outbox.status='skipped'(不再重试)。"""
        from services import notifications
        notif_id = await notifications.send(
            user_id=8005, notif_type="ready",
            payload={"file_code": "OUT005"},
        )
        # 手动将 max_attempts 设为 1,便于测试
        await store._db.execute(
            "UPDATE notification_outbox SET max_attempts = 1 WHERE notif_id = ?",
            (notif_id,),
        )
        await store._db.commit()
        # 第一次失败 → attempts=1 >= max_attempts=1 → 标记 skipped
        await notifications.record_notification_receipt(
            notif_id=notif_id, user_id=8005,
            channel="telegram", status="failed",
            error="persistent_error",
        )
        rows = await store._db.execute_fetchall(
            "SELECT status, attempts FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        assert rows[0][0] == "skipped"
        assert rows[0][1] == 1

    @pytest.mark.asyncio
    async def test_get_pending_outbox(self, store, reset_outbox_schema):
        """get_pending_outbox 返回 pending/failed 记录。"""
        from services import notifications
        await notifications.send(8006, "ready", {"file_code": "P001"})
        await notifications.send(8007, "ready", {"file_code": "P002"})
        pending = await notifications.get_pending_outbox(limit=10)
        assert len(pending) == 2
        assert all(p["status"] == "pending" for p in pending)

    @pytest.mark.asyncio
    async def test_mark_outbox_skipped(self, store, reset_outbox_schema):
        """mark_outbox_skipped 手动跳过投递。"""
        from services import notifications
        notif_id = await notifications.send(8008, "ready", {"file_code": "S001"})
        # 找到对应的 outbox_id
        rows = await store._db.execute_fetchall(
            "SELECT id FROM notification_outbox WHERE notif_id = ?",
            (notif_id,),
        )
        outbox_id = rows[0][0]
        ok = await notifications.mark_outbox_skipped(outbox_id, reason="用户已注销")
        assert ok is True
        # 验证状态
        rows = await store._db.execute_fetchall(
            "SELECT status, last_error FROM notification_outbox WHERE id = ?",
            (outbox_id,),
        )
        assert rows[0][0] == "skipped"
        assert "用户已注销" in rows[0][1]

    @pytest.mark.asyncio
    async def test_get_outbox_stats(self, store, reset_outbox_schema):
        """get_outbox_stats 返回各状态计数。"""
        from services import notifications
        # 创建 3 条通知
        n1 = await notifications.send(8009, "ready", {"file_code": "ST001"})
        n2 = await notifications.send(8010, "ready", {"file_code": "ST002"})
        n3 = await notifications.send(8011, "ready", {"file_code": "ST003"})
        # 标记 n1 为 delivered
        await notifications.record_notification_receipt(n1, 8009, "telegram", "delivered")
        # 标记 n2 为 failed
        await notifications.record_notification_receipt(n2, 8010, "telegram", "failed", "err")
        stats = await notifications.get_outbox_stats()
        assert stats["total"] == 3
        assert stats["pending"] == 1  # n3
        assert stats["delivered"] == 1  # n1
        assert stats["failed"] == 1  # n2
        # delivery_success_rate = 1 / (1+1+0) = 0.5
        assert 0.4 < stats["delivery_success_rate"] < 0.6

    @pytest.mark.asyncio
    async def test_dispatch_notification_dedup_still_works(
        self, store, reset_outbox_schema
    ):
        """dispatch_notification dedup_key 去重仍然有效(R45 不破坏 R41 行为)。"""
        from services import notifications
        id1 = await notifications.dispatch_notification(
            user_id=8020, type="ready",
            content={"file_code": "DD001"},
            dedup_key="task_complete:8020",
        )
        assert id1 > 0
        # 同一 dedup_key 1 小时内不重复
        id2 = await notifications.dispatch_notification(
            user_id=8020, type="ready",
            content={"file_code": "DD001"},
            dedup_key="task_complete:8020",
        )
        assert id2 == 0


# ════════════════════════════════════════════════════════════════
# 7. Maintenance check_maintenance_at_entry 测试
# ════════════════════════════════════════════════════════════════

class TestMaintenanceCheckAtEntry:
    """R45 第 16 节: check_maintenance_at_entry 统一入口检查。"""

    @pytest.mark.asyncio
    async def test_allowed_when_maintenance_off(self, store, reset_maintenance_cache):
        """维护模式关闭 → allowed=True。"""
        from services import maintenance_mode
        # 确保维护关闭
        await maintenance_mode.disable(ended_by=0, force=True)
        result = await maintenance_mode.check_maintenance_at_entry("上传文件")
        assert result["allowed"] is True
        assert result["maintenance_enabled"] is False
        assert result["reason"] == ""
        assert result["action"] == "上传文件"

    @pytest.mark.asyncio
    async def test_denied_when_maintenance_on(self, store, reset_maintenance_cache):
        """维护模式开启 → allowed=False,reason 提示维护中。"""
        from services import maintenance_mode
        await maintenance_mode.enable("测试维护", started_by=100)
        result = await maintenance_mode.check_maintenance_at_entry("解码文件")
        assert result["allowed"] is False
        assert result["maintenance_enabled"] is True
        assert "维护中" in result["reason"]
        assert result["action"] == "解码文件"

    @pytest.mark.asyncio
    async def test_fail_closed_on_db_unavailable(self, reset_maintenance_cache):
        """DB 不可达且无缓存 → fail-closed(allowed=False)。"""
        from services import maintenance_mode
        # 模拟 DB 未初始化(使用 mock store)
        from database import cache_store as cs
        original_store = cs._store
        try:
            mock_store = MagicMock()
            mock_store._db = None  # 模拟 DB 不可用
            cs._store = mock_store
            # 重置 maintenance 缓存(无 last_known)
            maintenance_mode._reset_cache_for_test()
            result = await maintenance_mode.check_maintenance_at_entry("CRDB 同步")
            assert result["allowed"] is False
            assert "无法判定" in result["reason"] or "暂不可用" in result["reason"]
        finally:
            cs._store = original_store

    @pytest.mark.asyncio
    async def test_returns_structured_fields(self, store, reset_maintenance_cache):
        """返回结果包含所有结构化字段。"""
        from services import maintenance_mode
        await maintenance_mode.disable(ended_by=0, force=True)
        result = await maintenance_mode.check_maintenance_at_entry("测试")
        # 必需字段
        assert "allowed" in result
        assert "maintenance_enabled" in result
        assert "reason" in result
        assert "source" in result
        assert "action" in result
        assert "last_checked" in result
        assert "error" in result


# ════════════════════════════════════════════════════════════════
# 8. Repair Console SAFE_ACTIONS + execute_repair 测试
# ════════════════════════════════════════════════════════════════

class TestRepairConsoleSafeActions:
    """R45 第 16 节: SAFE_ACTIONS 白名单 + execute_repair。"""

    def test_is_safe_action_whitelist(self):
        """白名单动作通过 is_safe_action 校验。"""
        from services import repair_console
        for action in repair_console.SAFE_ACTIONS:
            assert repair_console.is_safe_action(action) is True, \
                f"{action} 应在白名单中"

    def test_is_safe_action_rejects_dangerous(self):
        """危险动作(execute_sql/eval)不在白名单中。"""
        from services import repair_console
        assert repair_console.is_safe_action("execute_sql") is False
        assert repair_console.is_safe_action("execute_python") is False
        assert repair_console.is_safe_action("eval") is False
        assert repair_console.is_safe_action("exec") is False
        assert repair_console.is_safe_action("") is False
        assert repair_console.is_safe_action("random_action") is False

    @pytest.mark.asyncio
    async def test_execute_repair_rejects_non_whitelist(self, store):
        """非白名单动作 → 抛 ValueError + 写拒绝审计。"""
        from services import repair_console
        with pytest.raises(ValueError, match="不在白名单中"):
            await repair_console.execute_repair(
                action="execute_sql",
                params={"query": "DROP TABLE users"},
                principal_id=100,
            )
        # 验证审计日志写入(repair_rejected_*)
        rows = await store._db.execute_fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'repair_rejected_%'"
        )
        assert len(rows) >= 1
        assert "repair_rejected_execute_sql" in [r[0] for r in rows if r]

    @pytest.mark.asyncio
    async def test_execute_repair_skip_outbox(self, store):
        """白名单动作 skip_outbox 成功执行。"""
        from services import repair_console
        # 先创建 dirty_outbox 记录
        await store._db.execute(
            """INSERT INTO dirty_outbox (table_name, pk, version, operation,
               payload, created_at, processed)
               VALUES ('test_table', '1', 1, 'INSERT', '{}', ?, 0)""",
            (datetime.now().isoformat(),),
        )
        await store._db.commit()
        # 查询 ID
        rows = await store._db.execute_fetchall(
            "SELECT id FROM dirty_outbox WHERE processed = 0"
        )
        ids = [r[0] for r in rows if r]
        assert len(ids) >= 1
        result = await repair_console.execute_repair(
            action="skip_outbox",
            params={"ids": ids, "reason": "测试跳过"},
            principal_id=100,
        )
        assert result["success"] is True
        assert result["action"] == "skip_outbox"
        assert result["affected_count"] >= 1
        assert result["audit_log_id"] > 0

    @pytest.mark.asyncio
    async def test_execute_repair_with_valid_approval(self, store):
        """提供有效 approval_action_id → approval_verified=True。"""
        from services import repair_console
        # 创建 command_executions 记录
        await _insert_command_execution(
            store, action_id="approval_001", status="executed",
        )
        # 先创建 dirty_outbox 记录
        await store._db.execute(
            """INSERT INTO dirty_outbox (table_name, pk, version, operation,
               payload, created_at, processed)
               VALUES ('test_table', '1', 1, 'INSERT', '{}', ?, 0)""",
            (datetime.now().isoformat(),),
        )
        await store._db.commit()
        rows = await store._db.execute_fetchall(
            "SELECT id FROM dirty_outbox WHERE processed = 0"
        )
        ids = [r[0] for r in rows if r]
        result = await repair_console.execute_repair(
            action="retry_outbox",
            params={"ids": ids},
            principal_id=100,
            approval_action_id="approval_001",
        )
        assert result["success"] is True
        assert result["approval_verified"] is True

    @pytest.mark.asyncio
    async def test_execute_repair_rejects_invalid_approval(self, store):
        """无效 approval_action_id(未审批) → 抛 PermissionError。"""
        from services import repair_console
        # 创建 status='executing'(未 executed)的 command_executions
        await _insert_command_execution(
            store, action_id="approval_002", status="executing",
        )
        with pytest.raises(PermissionError, match="未通过验证"):
            await repair_console.execute_repair(
                action="skip_outbox",
                params={"ids": [1], "reason": "test"},
                principal_id=100,
                approval_action_id="approval_002",
            )

    @pytest.mark.asyncio
    async def test_execute_repair_rejects_nonexistent_approval(self, store):
        """不存在的 approval_action_id → 抛 PermissionError。"""
        from services import repair_console
        with pytest.raises(PermissionError, match="未通过验证"):
            await repair_console.execute_repair(
                action="skip_outbox",
                params={"ids": [1], "reason": "test"},
                principal_id=100,
                approval_action_id="nonexistent_approval_xxx",
            )


# ════════════════════════════════════════════════════════════════
# 9. Repair Console causal_chain + payload_hash 测试
# ════════════════════════════════════════════════════════════════

class TestRepairConsoleCausalChain:
    """R45 第 16 节: get_causal_chain + compute_payload_hash。"""

    def test_compute_payload_hash_dict(self):
        """compute_payload_hash 对 dict 返回确定性哈希。"""
        from services import repair_console
        h1 = repair_console.compute_payload_hash({"a": 1, "b": 2})
        h2 = repair_console.compute_payload_hash({"b": 2, "a": 1})  # 顺序不同
        assert h1 == h2  # sort_keys=True 保证顺序无关
        assert len(h1) == 16  # 默认 max_length=16

    def test_compute_payload_hash_different_payloads(self):
        """不同 payload 产生不同哈希。"""
        from services import repair_console
        h1 = repair_console.compute_payload_hash({"a": 1})
        h2 = repair_console.compute_payload_hash({"a": 2})
        assert h1 != h2

    def test_compute_payload_hash_none(self):
        """None payload 返回非空哈希。"""
        from services import repair_console
        h = repair_console.compute_payload_hash(None)
        assert h != ""
        assert h != "hash_error"

    def test_compute_payload_hash_custom_algorithm(self):
        """支持自定义算法(sha1/md5)。"""
        from services import repair_console
        h_sha256 = repair_console.compute_payload_hash({"x": 1}, algorithm="sha256")
        h_sha1 = repair_console.compute_payload_hash({"x": 1}, algorithm="sha1")
        assert h_sha256 != h_sha1

    @pytest.mark.asyncio
    async def test_get_causal_chain_empty_trace_id(self, store):
        """空 trace_id → 返回空结果。"""
        from services import repair_console
        result = await repair_console.get_causal_chain("")
        assert result["total"] == 0
        assert result["events"] == []

    @pytest.mark.asyncio
    async def test_get_causal_chain_finds_audit_log(self, store):
        """trace_id 出现在 audit_log.details → 因果链包含该记录。"""
        from services import repair_console
        # 写入包含 trace_id 的审计日志
        await store._db.execute(
            """INSERT INTO audit_log
               (actor_id, actor_type, action, target_type, target_id,
                details, ip_addr, created_at)
               VALUES (?, 'admin', 'test_action', 'test', '1',
                ?, '', ?)""",
            (100, f"操作 trace_id=TRACE_001 完成",
             datetime.now().isoformat()),
        )
        await store._db.commit()
        result = await repair_console.get_causal_chain("TRACE_001")
        assert result["trace_id"] == "TRACE_001"
        assert result["total"] >= 1
        audit_events = [e for e in result["events"] if e["source"] == "audit_log"]
        assert len(audit_events) >= 1
        assert "details_hash" in audit_events[0]["summary"]

    @pytest.mark.asyncio
    async def test_get_causal_chain_finds_tasks(self, store):
        """trace_id 出现在 tasks.trace_id → 因果链包含任务记录。"""
        from services import repair_console
        # 直接插入带 trace_id 的任务
        now = datetime.now().isoformat()
        await store._db.execute(
            """INSERT INTO tasks
               (task_type, user_id, status, progress, payload, result,
                error, trace_id, created_at, updated_at)
               VALUES ('upload', 9001, 'completed', 100, '{}', '{}',
                '', 'TRACE_TASK_001', ?, ?)""",
            (now, now),
        )
        await store._db.commit()
        result = await repair_console.get_causal_chain("TRACE_TASK_001")
        task_events = [e for e in result["events"] if e["source"] == "tasks"]
        assert len(task_events) >= 1
        assert "task:upload" in task_events[0]["action"]

    @pytest.mark.asyncio
    async def test_format_causal_chain_output(self, store):
        """format_causal_chain 输出可读文本。"""
        from services import repair_console
        await store._db.execute(
            """INSERT INTO audit_log
               (actor_id, actor_type, action, target_type, target_id,
                details, ip_addr, created_at)
               VALUES (?, 'admin', 'test', 'test', '1',
                ?, '', ?)""",
            (100, "trace_id=TRACEFMT001", datetime.now().isoformat()),
        )
        await store._db.commit()
        chain = await repair_console.get_causal_chain("TRACEFMT001")
        text = repair_console.format_causal_chain(chain)
        assert "TRACEFMT001" in text
        assert "audit_log" in text
        # 不应包含 markdown bold 标记(避免 Telegram 解析问题)
        # 注:不检查下划线,因 trace_id 本身可能含 _
        assert "*" not in text  # 无 bold 标记

    @pytest.mark.asyncio
    async def test_format_causal_chain_empty(self):
        """空因果链 → 提示无关联事件。"""
        from services import repair_console
        text = repair_console.format_causal_chain(
            {"trace_id": "EMPTY", "total": 0, "events": []}
        )
        assert "无关联事件" in text


# ════════════════════════════════════════════════════════════════
# 10. Maintenance workflow 失败保持 enabled 测试(R45 验证)
# ════════════════════════════════════════════════════════════════

class TestMaintenanceWorkflowFailureKeepsEnabled:
    """R45 第 16 节: drain/backup/migration/verify 失败时保持 enabled。"""

    @pytest.mark.asyncio
    async def test_drain_failure_keeps_enabled_and_sets_pending(
        self, store, reset_maintenance_cache
    ):
        """drain 失败 → workflow 失败 + 保持 enabled + recover_status=pending。"""
        from services import maintenance_mode
        # Mock drain_queues 返回失败
        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": False,
                "remaining_outbox": 5,
                "remaining_jobs": 0,
                "timeout": True,
            }),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试 drain 失败",
                started_by=100,
                auto_disable=True,
            )
        assert result["success"] is False
        assert result["maintenance_kept_enabled"] is True
        # 验证 maintenance 仍为 enabled
        assert await maintenance_mode.is_enabled() is True
        # 验证 recover_status='pending'
        rows = await store._db.execute_fetchall(
            "SELECT recover_status FROM maintenance_state WHERE id = 1"
        )
        assert rows[0][0] == "pending"

    @pytest.mark.asyncio
    async def test_check_maintenance_at_entry_during_workflow_failure(
        self, store, reset_maintenance_cache
    ):
        """workflow 失败后,check_maintenance_at_entry 应返回 allowed=False。"""
        from services import maintenance_mode
        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": False,
                "remaining_outbox": 1,
                "remaining_jobs": 0,
                "timeout": True,
            }),
        ):
            await maintenance_mode.execute_maintenance_workflow(
                reason="测试入口检查",
                started_by=100,
                auto_disable=True,
            )
        # 此时维护应保持开启
        result = await maintenance_mode.check_maintenance_at_entry("新上传")
        assert result["allowed"] is False
        assert result["maintenance_enabled"] is True
