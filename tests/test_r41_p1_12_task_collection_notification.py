"""R41 P1-12: TaskCenter / Notification / Collection 用户接线测试。

被测目标:
- services/task_center.py: record_task (一站式任务记录)
- services/notifications.py: dispatch_notification (幂等去重投递)
- services/collections.py: resolve_collection (集合权限校验)
- bots/up_bot.py / bots/idx_bot.py / bots/dsp_bot.py: record_task 接线(静态分析)

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db),
  通过设置 _cs_module._store 让所有模块的 get_cache_store() 返回测试 store。
- 通过直接 INSERT 插入测试数据(用户 / 文件记录 / 集合 + 集合项)。
- Bot 接线通过静态源码分析验证(避免完整 Telegram 环境依赖)。
"""
import inspect
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

# Mock telegram 模块(python-telegram-bot 未安装于测试环境)
# collections.py → code_generator → file_utils → telegram
# 在导入 services.collections 之前注入,避免 ModuleNotFoundError
# R41 修复:全量测试中可能其他测试已 import telegram 真实模块(或失败缓存),
# 需要无条件覆盖,强制重新加载 services.collections
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
# 清理已加载的 services.collections 缓存(若已 import,需要重新加载以使用 mock)
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("services.collections") or mod_name == "services.collections":
        del sys.modules[mod_name]

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
    content_reports)的 get_cache_store() 返回测试 store,无需逐模块 monkeypatch。
    """
    tmpdir = tempfile.mkdtemp(prefix="r41_p1_12_test_")
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


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_file_record(store, file_code: str, uploader_id: int = 0,
                               deleted: bool = False):
    """插入测试文件记录到 file_records_local。"""
    now = datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO file_records_local
           (file_code, uploader_id, status, deleted_at, crdb_synced)
           VALUES (?, ?, ?, ?, 1)""",
        (file_code, uploader_id,
         "deleted" if deleted else "active",
         now if deleted else None),
    )
    await store._db.commit()


async def _count_notifications(store, user_id: int, ntype: str = None) -> int:
    """统计用户的通知数量(可按类型过滤)。"""
    if ntype:
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND type = ?",
            (user_id, ntype),
        )
    else:
        rows = await store._db.execute_fetchall(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
            (user_id,),
        )
    return int(rows[0][0]) if rows else 0


# ════════════════════════════════════════════════════════════════
# 1. TaskCenter.record_task 测试
# ════════════════════════════════════════════════════════════════

class TestRecordTask:
    """task_center.record_task 一站式任务记录。"""

    @pytest.mark.asyncio
    async def test_record_completed_task(self, store):
        """record_task(status='completed') 创建并完成任务。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1001,
            task_type="upload",
            status="completed",
            metadata={"file_code": "ABC123", "file_size": 1024},
        )
        assert task_id > 0
        # 验证任务状态
        task = await task_center.get_task(task_id)
        assert task is not None
        assert task["status"] == "completed"
        assert task["progress"] == 100
        # 验证 result 包含 metadata
        result = task["result"]
        assert result["file_code"] == "ABC123"
        assert result["file_size"] == 1024

    @pytest.mark.asyncio
    async def test_record_failed_task(self, store):
        """record_task(status='failed') 创建并失败任务。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1002,
            task_type="delivery",
            status="failed",
            metadata={"file_code": "XYZ789", "error": "channel_unavailable"},
        )
        assert task_id > 0
        task = await task_center.get_task(task_id)
        assert task["status"] == "failed"
        assert "channel_unavailable" in task["error"]

    @pytest.mark.asyncio
    async def test_record_pending_task(self, store):
        """record_task(status='pending') 创建待处理任务。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1003,
            task_type="index",
            status="pending",
            metadata={"file_code": "DEF456"},
        )
        assert task_id > 0
        task = await task_center.get_task(task_id)
        assert task["status"] == "pending"
        assert task["progress"] == 0

    @pytest.mark.asyncio
    async def test_unknown_task_type_fallback(self, store):
        """未知 task_type 回退到 'index'。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1004,
            task_type="unknown_type",
            status="completed",
            metadata={"key": "value"},
        )
        assert task_id > 0
        task = await task_center.get_task(task_id)
        assert task["task_type"] == "index"

    @pytest.mark.asyncio
    async def test_unknown_status_fallback(self, store):
        """未知 status 回退到 'pending'。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1005,
            task_type="upload",
            status="invalid_status",
            metadata={},
        )
        assert task_id > 0
        task = await task_center.get_task(task_id)
        assert task["status"] == "pending"

    @pytest.mark.asyncio
    async def test_record_cancelled_task(self, store):
        """record_task(status='cancelled') 创建并取消任务。"""
        from services import task_center
        task_id = await task_center.record_task(
            user_id=1006,
            task_type="copy",
            status="cancelled",
            metadata={"reason": "user_request"},
        )
        assert task_id > 0
        task = await task_center.get_task(task_id)
        assert task["status"] == "cancelled"


# ════════════════════════════════════════════════════════════════
# 2. Notification.dispatch_notification 测试
# ════════════════════════════════════════════════════════════════

class TestDispatchNotification:
    """notifications.dispatch_notification 幂等去重投递。"""

    @pytest.mark.asyncio
    async def test_dispatch_without_dedup_key(self, store):
        """无 dedup_key → 每次都发送(等同 send())。"""
        from services import notifications
        id1 = await notifications.dispatch_notification(
            user_id=2001, type="ready",
            content={"file_code": "AAA"},
            dedup_key="",
        )
        id2 = await notifications.dispatch_notification(
            user_id=2001, type="ready",
            content={"file_code": "BBB"},
            dedup_key="",
        )
        assert id1 > 0
        assert id2 > 0
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_dispatch_dedup_same_key(self, store):
        """同一 dedup_key 1 小时内不重复投递。"""
        from services import notifications
        id1 = await notifications.dispatch_notification(
            user_id=2002, type="ready",
            content={"file_code": "CCC"},
            dedup_key="task_complete:999",
        )
        id2 = await notifications.dispatch_notification(
            user_id=2002, type="ready",
            content={"file_code": "CCC"},
            dedup_key="task_complete:999",
        )
        assert id1 > 0
        assert id2 == 0, "同一 dedup_key 1 小时内应被去重(返回 0)"

    @pytest.mark.asyncio
    async def test_dispatch_different_dedup_keys(self, store):
        """不同 dedup_key 不去重,都发送。"""
        from services import notifications
        id1 = await notifications.dispatch_notification(
            user_id=2003, type="ready",
            content={"file_code": "DDD"},
            dedup_key="task_complete:1001",
        )
        id2 = await notifications.dispatch_notification(
            user_id=2003, type="ready",
            content={"file_code": "EEE"},
            dedup_key="task_complete:1002",
        )
        assert id1 > 0
        assert id2 > 0
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_dispatch_dedup_different_users(self, store):
        """同一 dedup_key 不同用户 → 都发送(去重按 user_id + dedup_key)。"""
        from services import notifications
        id1 = await notifications.dispatch_notification(
            user_id=2004, type="ready",
            content={"file_code": "FFF"},
            dedup_key="shared_key_xyz",
        )
        id2 = await notifications.dispatch_notification(
            user_id=2005, type="ready",
            content={"file_code": "FFF"},
            dedup_key="shared_key_xyz",
        )
        assert id1 > 0
        assert id2 > 0, "同一 dedup_key 不同用户不应被去重"

    @pytest.mark.asyncio
    async def test_dispatch_dedup_key_stored_in_payload(self, store):
        """dedup_key 被存储在通知 payload 的 _dedup_key 字段中。"""
        from services import notifications
        notif_id = await notifications.dispatch_notification(
            user_id=2006, type="ready",
            content={"file_code": "GGG"},
            dedup_key="verify_payload_key",
        )
        assert notif_id > 0
        # 查询通知 payload
        rows = await store._db.execute_fetchall(
            "SELECT payload FROM notifications WHERE id = ?",
            (notif_id,),
        )
        assert len(rows) == 1
        import json as _json
        payload = _json.loads(rows[0][0])
        assert payload.get("_dedup_key") == "verify_payload_key"


# ════════════════════════════════════════════════════════════════
# 3. Collection.resolve_collection 测试
# ════════════════════════════════════════════════════════════════

class TestResolveCollection:
    """collections.resolve_collection 集合权限校验。"""

    @pytest.mark.asyncio
    async def test_owner_has_full_access(self, store):
        """集合 owner 访问自己的集合 → allowed=True,所有项 has_access=True。"""
        from services import collections
        # 创建集合(owner=3001)
        coll = await collections.create_collection("我的合集", owner_id=3001)
        coll_id = coll["id"]
        # 插入文件记录(uploader_id 为其他用户)
        await _insert_file_record(store, "FILE001", uploader_id=9999)
        await _insert_file_record(store, "FILE002", uploader_id=8888)
        # 添加文件到集合
        await collections.add_files(coll_id, ["FILE001", "FILE002"])
        # owner 解析集合
        result = await collections.resolve_collection(user_id=3001, collection_id=coll_id)
        assert result["allowed"] is True
        assert result["owner_id"] == 3001
        assert len(result["items"]) == 2
        assert all(it["has_access"] for it in result["items"])
        assert result["denied_items"] == []

    @pytest.mark.asyncio
    async def test_non_owner_with_matching_uploader(self, store):
        """非 owner 用户访问集合,文件 uploader_id == user_id → 允许。"""
        from services import collections
        coll = await collections.create_collection("共享合集", owner_id=3002)
        coll_id = coll["id"]
        # FILE001 上传者是 3003,FILE002 上传者是 4004
        await _insert_file_record(store, "FILE003", uploader_id=3003)
        await _insert_file_record(store, "FILE004", uploader_id=4004)
        await collections.add_files(coll_id, ["FILE003", "FILE004"])
        # 3003 访问:FILE003 允许,FILE004 拒绝
        result = await collections.resolve_collection(user_id=3003, collection_id=coll_id)
        assert result["allowed"] is False
        assert len(result["denied_items"]) == 1
        assert result["denied_items"][0]["file_code"] == "FILE004"
        # FILE003 应有访问权限
        file003_item = next(it for it in result["items"] if it["file_code"] == "FILE003")
        assert file003_item["has_access"] is True
        file004_item = next(it for it in result["items"] if it["file_code"] == "FILE004")
        assert file004_item["has_access"] is False

    @pytest.mark.asyncio
    async def test_non_owner_no_access(self, store):
        """非 owner 用户访问集合,所有文件 uploader_id 都不匹配 → 拒绝。"""
        from services import collections
        coll = await collections.create_collection("私人合集", owner_id=3004)
        coll_id = coll["id"]
        await _insert_file_record(store, "FILE005", uploader_id=3004)
        await _insert_file_record(store, "FILE006", uploader_id=3004)
        await collections.add_files(coll_id, ["FILE005", "FILE006"])
        # 3005 访问(不是 owner,也不是 uploader)
        result = await collections.resolve_collection(user_id=3005, collection_id=coll_id)
        assert result["allowed"] is False
        assert len(result["denied_items"]) == 2
        assert all(not it["has_access"] for it in result["items"])

    @pytest.mark.asyncio
    async def test_collection_not_found(self, store):
        """集合不存在 → allowed=False, error='集合不存在'。"""
        from services import collections
        result = await collections.resolve_collection(
            user_id=3006, collection_id=99999,
        )
        assert result["allowed"] is False
        assert "不存在" in result["error"]

    @pytest.mark.asyncio
    async def test_disabled_collection_denied(self, store):
        """集合状态非 active → allowed=False。"""
        from services import collections
        coll = await collections.create_collection("禁用合集", owner_id=3007)
        coll_id = coll["id"]
        await _insert_file_record(store, "FILE007", uploader_id=3007)
        await collections.add_files(coll_id, ["FILE007"])
        # 手动将集合状态改为 disabled
        await store._db.execute(
            "UPDATE collections SET status = 'disabled' WHERE id = ?",
            (coll_id,),
        )
        await store._db.commit()
        # owner 访问(但因为集合已禁用,应拒绝)
        result = await collections.resolve_collection(user_id=3007, collection_id=coll_id)
        assert result["allowed"] is False

    @pytest.mark.asyncio
    async def test_deleted_file_status(self, store):
        """集合中包含已删除文件 → 文件状态为 deleted。"""
        from services import collections
        coll = await collections.create_collection("含删除合集", owner_id=3008)
        coll_id = coll["id"]
        # FILE008 正常,FILE009 已软删除
        await _insert_file_record(store, "FILE008", uploader_id=3008)
        await _insert_file_record(store, "FILE009", uploader_id=3008, deleted=True)
        await collections.add_files(coll_id, ["FILE008", "FILE009"])
        result = await collections.resolve_collection(user_id=3008, collection_id=coll_id)
        assert result["allowed"] is True
        file009_item = next(it for it in result["items"] if it["file_code"] == "FILE009")
        assert file009_item["status"] == "deleted"


# ════════════════════════════════════════════════════════════════
# 4. Bot 接线测试(静态源码分析)
# ════════════════════════════════════════════════════════════════

class TestBotWiring:
    """验证 up_bot / idx_bot / dsp_bot 调用 task_center.record_task。

    通过静态源码分析验证接线,避免完整 Telegram 环境依赖。
    """

    @staticmethod
    def _read_bot_source(bot_name: str) -> str:
        """读取 bot 源码文本。"""
        bot_file = Path(__file__).resolve().parent.parent / "bots" / f"{bot_name}.py"
        if not bot_file.exists():
            pytest.skip(f"{bot_name}.py 不存在")
        return bot_file.read_text(encoding="utf-8")

    def test_up_bot_imports_task_center(self):
        """up_bot 导入 task_center 模块。"""
        source = self._read_bot_source("up_bot")
        assert "task_center" in source, "up_bot 应导入 task_center"

    def test_up_bot_calls_record_task(self):
        """up_bot 在上传成功后调用 task_center.record_task。"""
        source = self._read_bot_source("up_bot")
        assert "task_center.record_task" in source, \
            "up_bot 应调用 task_center.record_task"
        # 验证使用 'upload' 任务类型
        assert "'upload'" in source or '"upload"' in source, \
            "up_bot 应使用 'upload' 任务类型调用 record_task"

    def test_idx_bot_imports_task_center(self):
        """idx_bot 导入 task_center 模块。"""
        source = self._read_bot_source("idx_bot")
        assert "task_center" in source, "idx_bot 应导入 task_center"

    def test_idx_bot_calls_record_task(self):
        """idx_bot 在解码成功后调用 task_center.record_task。"""
        source = self._read_bot_source("idx_bot")
        assert "task_center.record_task" in source, \
            "idx_bot 应调用 task_center.record_task"
        # 验证使用 'index' 或 'decode' 任务类型
        assert "'index'" in source or '"index"' in source, \
            "idx_bot 应使用 'index' 任务类型调用 record_task"

    def test_dsp_bot_imports_task_center(self):
        """dsp_bot 导入 task_center 模块。"""
        source = self._read_bot_source("dsp_bot")
        assert "task_center" in source, "dsp_bot 应导入 task_center"

    def test_dsp_bot_calls_record_task(self):
        """dsp_bot 在派送成功后调用 task_center.record_task。"""
        source = self._read_bot_source("dsp_bot")
        assert "task_center.record_task" in source, \
            "dsp_bot 应调用 task_center.record_task"
        # 验证使用 'delivery' 或 'deliver' 任务类型
        assert "'delivery'" in source or '"delivery"' in source or \
               "'deliver'" in source or '"deliver"' in source, \
            "dsp_bot 应使用 'delivery' 任务类型调用 record_task"
