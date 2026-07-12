"""R36 Batch 1: B0-1 ReplicaAwareResolver 成为真实投递主路径测试。

被测目标:
- ``database.cache_store.CacheStore`` 的 ``local_job_queue`` 表新增 3 列:
  ``group_id`` / ``file_unique_id`` / ``media_group_id``(结构化副本信息)
- ``database.cache_store.CacheStore.insert_local_job`` 支持新字段写入
- ``database.cache_store.CacheStore.get_local_job_by_crdb_id`` /
  ``get_local_pending_jobs`` 返回新字段
- ``database.cache_store.CacheStore.get_local_job_with_replica_info`` 新方法
- ``database.session.enqueue_job`` 接受并透传 ``group_id`` / ``file_unique_id``
  / ``media_group_id`` 参数
- ``database.session.JobResult`` 携带结构化副本信息字段
- ``bots.dsp_bot._extract_replica_info`` 优先从结构化字段读取(返回 4-tuple)
- ``bots.dsp_bot._raw_jobs_to_results`` 透传结构化字段到 JobResult
- ``bots.dsp_bot._process_single_job`` 对新 job 缺 group_id 进入 retry(fail-closed)

测试策略:
- 使用真实 SQLite 临时文件数据库,验证 DDL 升级幂等性
- 使用 mock job 对象,验证 _extract_replica_info 的结构化字段优先级
- 使用真实 SQLite 验证 insert/get 全链透传
- 若 bots.dsp_bot 因依赖缺失无法导入,相关测试优雅跳过

对应 R36 B0-1 要求:
- group_id/file_unique_id/media_group_id 全链透传
- ReplicaAwareResolver 成为默认路径
- 缺 group_id 的新 job 进入 retry,不静默走旧拓扑猜测
"""
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ── Fixture: 真实 SQLite 临时数据库 ──────────────────────────────

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r36_batch1_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── 尝试导入 bots.dsp_bot(依赖 telegram 等) ────────────────────

_dsp_bot_available = False

try:
    import bots.dsp_bot as dsp_bot_module
    _dsp_bot_available = True
except Exception:
    _dsp_bot_available = False


# ════════════════════════════════════════════════════════════════
# 1. SQLite 表结构升级测试(DDL 幂等性)
# ════════════════════════════════════════════════════════════════

class TestLocalJobQueueSchema:
    """R36 B0-1: local_job_queue 表新增 3 列的 DDL 升级。"""

    @pytest.mark.asyncio
    async def test_new_columns_exist_after_init(self, real_store):
        """init() 后 local_job_queue 表应包含 group_id/file_unique_id/media_group_id 列。"""
        # PRAGMA table_info 返回 [(cid, name, type, notnull, dflt_value, pk), ...]
        rows = await real_store._db.execute_fetchall(
            "PRAGMA table_info(local_job_queue)"
        )
        column_names = {r[1] for r in rows}
        assert "group_id" in column_names, "缺少 group_id 列"
        assert "file_unique_id" in column_names, "缺少 file_unique_id 列"
        assert "media_group_id" in column_names, "缺少 media_group_id 列"

    @pytest.mark.asyncio
    async def test_default_values_for_new_columns(self, real_store):
        """新列应有正确的默认值:group_id=0, file_unique_id='', media_group_id=''。"""
        # 插入一条不指定新列的 job(模拟旧代码写入)
        await real_store._db.execute(
            """INSERT INTO local_job_queue
            (crdb_id, code, target_user_id, storage_channel_id,
             storage_msg_ids, batch_file_meta, task_type, status,
             retry_count, protect_content, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (-990001, "TESTCODE_OLD", 1001, 2001, "[]", "",
             "single", "pending", 0, 0, "2024-01-01T00:00:00Z"),
        )
        await real_store._db.commit()
        rows = await real_store._db.execute_fetchall(
            "SELECT group_id, file_unique_id, media_group_id "
            "FROM local_job_queue WHERE crdb_id = ?",
            (-990001,),
        )
        assert rows, "查询应返回一行"
        gid, fuid, mgid = rows[0]
        assert gid == 0, f"group_id 默认值应为 0,实际: {gid}"
        assert fuid == "", f"file_unique_id 默认值应为空串,实际: {fuid!r}"
        assert mgid == "", f"media_group_id 默认值应为空串,实际: {mgid!r}"

    @pytest.mark.asyncio
    async def test_init_idempotent_with_repeated_alter(self, real_store):
        """多次 init() 不应因 ALTER TABLE 重复列错误失败。"""
        # 第一次 init 已在 fixture 完成,再调一次 init 验证幂等
        await real_store.init()
        # 验证表仍可正常插入
        await real_store._db.execute(
            "INSERT INTO local_job_queue (crdb_id, code, target_user_id, "
            "storage_channel_id) VALUES (?, ?, ?, ?)",
            (-990002, "IDEMPOTENT_TEST", 1002, 2002),
        )
        await real_store._db.commit()
        rows = await real_store._db.execute_fetchall(
            "SELECT group_id FROM local_job_queue WHERE crdb_id = ?",
            (-990002,),
        )
        assert rows and rows[0][0] == 0


# ════════════════════════════════════════════════════════════════
# 2. insert_local_job / get_local_job 全链透传测试
# ════════════════════════════════════════════════════════════════

class TestInsertAndGetLocalJob:
    """R36 B0-1: 结构化副本信息字段的全链写入与读取。"""

    @pytest.mark.asyncio
    async def test_insert_with_replica_info_fields(self, real_store):
        """insert_local_job 写入 group_id/file_unique_id/media_group_id 后可读回。"""
        local_id = await real_store.insert_local_job({
            "code": "R36TEST1",
            "target_user_id": 10001,
            "storage_channel_id": 20001,
            "storage_msg_ids": "[1001]",
            "batch_file_meta": "[]",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            "group_id": 3,
            "file_unique_id": "fuid-r36-001",
            "media_group_id": "mgid-r36-001",
        })
        assert local_id < 0  # 临时负数 ID

        job = await real_store.get_local_job_by_crdb_id(local_id)
        assert job is not None
        assert job["group_id"] == 3
        assert job["file_unique_id"] == "fuid-r36-001"
        assert job["media_group_id"] == "mgid-r36-001"
        assert job["code"] == "R36TEST1"

    @pytest.mark.asyncio
    async def test_insert_without_replica_info_defaults_to_zero(self, real_store):
        """不传新字段时默认值为 0/空串(向后兼容旧调用方)。"""
        local_id = await real_store.insert_local_job({
            "code": "R36TEST2",
            "target_user_id": 10002,
            "storage_channel_id": 20002,
            "storage_msg_ids": "[1002]",
            "batch_file_meta": "",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            # 不传 group_id/file_unique_id/media_group_id
        })
        job = await real_store.get_local_job_by_crdb_id(local_id)
        assert job is not None
        assert job["group_id"] == 0
        assert job["file_unique_id"] == ""
        assert job["media_group_id"] == ""

    @pytest.mark.asyncio
    async def test_get_local_job_with_replica_info_method(self, real_store):
        """get_local_job_with_replica_info 返回结构化字段(等价于 get_local_job_by_crdb_id)。"""
        local_id = await real_store.insert_local_job({
            "code": "R36TEST3",
            "target_user_id": 10003,
            "storage_channel_id": 20003,
            "storage_msg_ids": "[1003]",
            "batch_file_meta": "[]",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            "group_id": 5,
            "file_unique_id": "fuid-r36-003",
            "media_group_id": "",
        })
        job = await real_store.get_local_job_with_replica_info(local_id)
        assert job is not None
        assert job["group_id"] == 5
        assert job["file_unique_id"] == "fuid-r36-003"
        assert job["media_group_id"] == ""

    @pytest.mark.asyncio
    async def test_get_local_pending_jobs_returns_replica_info(self, real_store):
        """get_local_pending_jobs 返回的 dict 应包含新字段。"""
        await real_store.insert_local_job({
            "code": "R36PEND1",
            "target_user_id": 20001,
            "storage_channel_id": 30001,
            "storage_msg_ids": "[2001]",
            "batch_file_meta": "",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            "group_id": 2,
            "file_unique_id": "fuid-pend-001",
            "media_group_id": "mgid-pend-001",
        })
        jobs = await real_store.get_local_pending_jobs(limit=10)
        assert len(jobs) >= 1
        r36_job = next((j for j in jobs if j["code"] == "R36PEND1"), None)
        assert r36_job is not None
        assert r36_job["group_id"] == 2
        assert r36_job["file_unique_id"] == "fuid-pend-001"
        assert r36_job["media_group_id"] == "mgid-pend-001"

    @pytest.mark.asyncio
    async def test_upsert_local_job_with_replica_info(self, real_store):
        """upsert_local_job(CRDB→SQLite 同步路径)正确写入新字段。"""
        await real_store.upsert_local_job({
            "id": 888001,
            "code": "R36UPSERT1",
            "target_user_id": 30001,
            "storage_channel_id": 40001,
            "storage_msg_ids": "[3001]",
            "batch_file_meta": "",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            "group_id": 4,
            "file_unique_id": "fuid-upsert-001",
            "media_group_id": "mgid-upsert-001",
        })
        job = await real_store.get_local_job_by_crdb_id(888001)
        assert job is not None
        assert job["group_id"] == 4
        assert job["file_unique_id"] == "fuid-upsert-001"
        assert job["media_group_id"] == "mgid-upsert-001"


# ════════════════════════════════════════════════════════════════
# 3. JobResult 携带结构化副本信息字段测试
# ════════════════════════════════════════════════════════════════

class TestJobResultReplicaInfo:
    """R36 B0-1: JobResult 类携带 group_id/file_unique_id/media_group_id。"""

    def test_job_result_default_values(self):
        """不传新字段时,JobResult 默认值为 0/空串(向后兼容)。"""
        from database import JobResult
        jr = JobResult(
            job_id=1, code="C", target_user_id=10,
            storage_channel_id=20, storage_msg_ids=[30],
            batch_file_meta="",
        )
        assert jr.group_id == 0
        assert jr.file_unique_id == ""
        assert jr.media_group_id == ""

    def test_job_result_with_replica_info(self):
        """传入新字段时,JobResult 正确存储。"""
        from database import JobResult
        jr = JobResult(
            job_id=1, code="C", target_user_id=10,
            storage_channel_id=20, storage_msg_ids=[30],
            batch_file_meta="",
            group_id=3,
            file_unique_id="fuid-jobresult-001",
            media_group_id="mgid-jobresult-001",
        )
        assert jr.group_id == 3
        assert jr.file_unique_id == "fuid-jobresult-001"
        assert jr.media_group_id == "mgid-jobresult-001"


# ════════════════════════════════════════════════════════════════
# 4. _extract_replica_info 结构化字段优先级测试(需要 dsp_bot)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用(需要 telegram / loguru 等依赖)",
)
class TestExtractReplicaInfoStructured:
    """R36 B0-1: _extract_replica_info 优先从结构化字段读取。"""

    def test_structured_fields_take_priority(self):
        """结构化字段非空时,优先返回结构化字段(不解析 batch_file_meta JSON)。"""
        job = MagicMock()
        job.file_unique_id = "fuid-struct-001"
        job.group_id = 3
        job.media_group_id = "mgid-struct-001"
        job.batch_file_meta = '[{"file_unique_id": "fuid-json-different", "group_id": 99}]'
        fuid, gid, mgid, is_new = dsp_bot_module._extract_replica_info(job)
        assert fuid == "fuid-struct-001"
        assert gid == 3
        assert mgid == "mgid-struct-001"
        assert is_new is True

    def test_structured_only_file_unique_id_group_id_zero(self):
        """结构化字段只有 file_unique_id,group_id=0 → 新 job 但数据不完整。"""
        job = MagicMock()
        job.file_unique_id = "fuid-incomplete-001"
        job.group_id = 0
        job.media_group_id = ""
        job.batch_file_meta = ""
        fuid, gid, mgid, is_new = dsp_bot_module._extract_replica_info(job)
        assert fuid == "fuid-incomplete-001"
        # gid 为 None(group_id=0 时,因为数据不完整)
        assert gid is None
        assert is_new is True

    def test_structured_only_group_id_nonzero(self):
        """结构化字段只有 group_id>0,file_unique_id 为空 → 仍判为新格式 job。"""
        job = MagicMock()
        job.file_unique_id = ""
        job.group_id = 2
        job.media_group_id = ""
        job.batch_file_meta = ""
        fuid, gid, mgid, is_new = dsp_bot_module._extract_replica_info(job)
        assert fuid == ""
        assert gid == 2
        assert is_new is True

    def test_old_job_fallback_to_json_parse(self):
        """旧 job(无结构化字段)fallback 到 batch_file_meta JSON 解析。"""
        job = MagicMock()
        job.file_unique_id = ""
        job.group_id = 0
        job.media_group_id = ""
        job.batch_file_meta = (
            '[{"file_unique_id": "fuid-old-001", "group_id": 1, '
            '"media_group_id": "mgid-old-001"}]'
        )
        fuid, gid, mgid, is_new = dsp_bot_module._extract_replica_info(job)
        assert fuid == "fuid-old-001"
        assert gid == 1
        assert mgid == "mgid-old-001"
        assert is_new is False

    def test_old_job_json_without_group_id(self):
        """旧 job JSON 无 group_id 字段 → gid 返回 None(向后兼容)。"""
        job = MagicMock()
        job.file_unique_id = ""
        job.group_id = 0
        job.media_group_id = ""
        job.batch_file_meta = '[{"file_unique_id": "fuid-legacy-001"}]'
        fuid, gid, mgid, is_new = dsp_bot_module._extract_replica_info(job)
        assert fuid == "fuid-legacy-001"
        assert gid is None
        assert is_new is False


# ════════════════════════════════════════════════════════════════
# 5. _raw_jobs_to_results 透传结构化字段测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestRawJobsToResultsReplicaInfo:
    """R36 B0-1: _raw_jobs_to_results 透传结构化字段到 JobResult。"""

    def test_raw_job_dict_with_replica_fields(self):
        """SQLite dict 含新字段时,JobResult 正确继承。"""
        raw_jobs = [{
            "crdb_id": 100001,
            "code": "R36RAW1",
            "target_user_id": 50001,
            "storage_channel_id": 60001,
            "storage_msg_ids": "[5001]",
            "batch_file_meta": "",
            "task_type": "single",
            "protect_content": False,
            "retry_count": 0,
            "group_id": 2,
            "file_unique_id": "fuid-raw-001",
            "media_group_id": "mgid-raw-001",
        }]
        results = dsp_bot_module._raw_jobs_to_results(raw_jobs)
        assert len(results) == 1
        jr = results[0]
        assert jr.group_id == 2
        assert jr.file_unique_id == "fuid-raw-001"
        assert jr.media_group_id == "mgid-raw-001"

    def test_raw_job_dict_missing_replica_fields_defaults(self):
        """SQLite dict 缺新字段时,JobResult 默认值为 0/空串。"""
        raw_jobs = [{
            "crdb_id": 100002,
            "code": "R36RAW2",
            "target_user_id": 50002,
            "storage_channel_id": 60002,
            "storage_msg_ids": "[5002]",
            "batch_file_meta": "",
            "task_type": "single",
            "protect_content": False,
            "retry_count": 0,
            # 缺 group_id/file_unique_id/media_group_id
        }]
        results = dsp_bot_module._raw_jobs_to_results(raw_jobs)
        assert len(results) == 1
        jr = results[0]
        assert jr.group_id == 0
        assert jr.file_unique_id == ""
        assert jr.media_group_id == ""


# ════════════════════════════════════════════════════════════════
# 6. _process_single_job fail-closed 行为测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestProcessSingleJobFailClosed:
    """R36 B0-1: 新 job 缺 group_id 或 Resolver 失败时进入 retry(fail-closed)。"""

    def _make_new_job_with_incomplete_group(self):
        """构造一个新格式 job(file_unique_id 已知但 group_id=0,数据不完整)。"""
        job = MagicMock()
        job.job_id = 70001
        job.code = "R36FAILCLOSED1"
        job.target_user_id = 80001
        job.storage_channel_id = 90001
        job.storage_msg_ids = [10001]
        job.batch_file_meta = json.dumps([{
            "type": "photo", "file_id": "x",
            "group_id": 0,  # 数据不完整
            "file_unique_id": "fuid-incomplete-001",
            "media_group_id": "",
        }])
        job.task_type = "single"
        job.protect_content = False
        job.retry_count = 0
        # 结构化字段(优先级高于 JSON)
        job.file_unique_id = "fuid-incomplete-001"
        job.group_id = 0
        job.media_group_id = ""
        return job

    @pytest.mark.asyncio
    async def test_new_job_missing_group_id_returns_false_for_retry(self):
        """新 job 缺 group_id(file_unique_id 已知但 group_id=0)应返回 False 触发 retry,
        不静默 fallback 到拓扑解析。"""
        job = self._make_new_job_with_incomplete_group()
        # mock store,避免真实 DB 操作
        store = MagicMock()
        store.get_delivery_receipts_by_job = AsyncMock(return_value=[])
        store.upsert_delivery_receipt = AsyncMock(return_value=True)
        store.mark_delivery_failed = AsyncMock(return_value=True)

        with patch("bots.dsp_bot._get_store_safe", return_value=store), \
             patch("bots.dsp_bot.resolve_delivery_channel") as mock_resolve, \
             patch("bots.dsp_bot.try_deliver") as mock_deliver:
            result = await dsp_bot_module._process_single_job(
                MagicMock(), job, bot_id=1
            )
        # 应返回 False(触发 retry)
        assert result is False
        # 不应调用 resolve_delivery_channel(不 fallback 到拓扑猜测)
        mock_resolve.assert_not_awaited()
        mock_deliver.assert_not_awaited()
        # 应记录失败回执
        store.mark_delivery_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_job_resolver_failure_returns_false_for_retry(self):
        """新 job Resolver 查询失败(返回 None)时应进入 retry,不 fallback。"""
        job = MagicMock()
        job.job_id = 70002
        job.code = "R36FAILCLOSED2"
        job.target_user_id = 80002
        job.storage_channel_id = 90002
        job.storage_msg_ids = [10002]
        job.batch_file_meta = ""
        job.task_type = "single"
        job.protect_content = False
        job.retry_count = 0
        # 结构化字段完整
        job.file_unique_id = "fuid-complete-001"
        job.group_id = 3
        job.media_group_id = ""

        store = MagicMock()
        store.get_delivery_receipts_by_job = AsyncMock(return_value=[])
        store.upsert_delivery_receipt = AsyncMock(return_value=True)
        store.mark_delivery_failed = AsyncMock(return_value=True)

        # mock Resolver 返回 None(查询失败)
        with patch("bots.dsp_bot._get_store_safe", return_value=store), \
             patch("bots.dsp_bot._try_replica_aware_resolve",
                   AsyncMock(return_value=None)) as mock_resolve_call, \
             patch("bots.dsp_bot.resolve_delivery_channel") as mock_topology, \
             patch("bots.dsp_bot.try_deliver") as mock_deliver:
            result = await dsp_bot_module._process_single_job(
                MagicMock(), job, bot_id=1
            )
        # 应返回 False(触发 retry,不 fallback 到拓扑)
        assert result is False
        mock_resolve_call.assert_awaited_once()
        # 不应调用拓扑解析
        mock_topology.assert_not_awaited()
        mock_deliver.assert_not_awaited()
        store.mark_delivery_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_job_resolver_success_uses_replica_channel(self):
        """新 job Resolver 命中时,使用 Resolver 返回的频道投递。"""
        job = MagicMock()
        job.job_id = 70003
        job.code = "R36RESOK"
        job.target_user_id = 80003
        job.storage_channel_id = 90003
        job.storage_msg_ids = [10003]
        job.batch_file_meta = ""
        job.task_type = "single"
        job.protect_content = False
        job.retry_count = 0
        job.file_unique_id = "fuid-success-001"
        job.group_id = 2
        job.media_group_id = ""

        store = MagicMock()
        store.get_delivery_receipts_by_job = AsyncMock(return_value=[])
        store.upsert_delivery_receipt = AsyncMock(return_value=True)
        store.confirm_delivery_receipt = AsyncMock(return_value=True)

        # mock Resolver 命中 (channel_id=12345, message_id=67890)
        with patch("bots.dsp_bot._get_store_safe", return_value=store), \
             patch("bots.dsp_bot._try_replica_aware_resolve",
                   AsyncMock(return_value=(12345, 67890))) as mock_resolve, \
             patch("bots.dsp_bot.try_deliver",
                   AsyncMock(return_value=99999)) as mock_deliver, \
             patch("bots.dsp_bot._should_preserve_caption",
                   AsyncMock(return_value=False)), \
             patch("bots.dsp_bot._build_delivery_caption",
                   AsyncMock(return_value="caption")), \
             patch("bots.dsp_bot._edit_sent_caption",
                   AsyncMock(return_value=None)), \
             patch("bots.dsp_bot.metrics") as mock_metrics:
            mock_metrics.record_send_success = AsyncMock()
            mock_metrics.record_processed = AsyncMock()
            result = await dsp_bot_module._process_single_job(
                MagicMock(), job, bot_id=1
            )
        # 应返回 True(投递成功)
        assert result is True
        mock_resolve.assert_awaited_once()
        # try_deliver 应使用 Resolver 返回的频道 12345 和消息 ID 67890
        mock_deliver.assert_awaited_once()
        call_args = mock_deliver.await_args
        # 第 3 个位置参数应为 replica_channel=12345,第 4 个为 replica_msg_id=67890
        assert call_args.args[2] == 12345
        assert call_args.args[3] == 67890

    @pytest.mark.asyncio
    async def test_old_job_falls_back_to_topology(self):
        """旧 job(无结构化字段,fuid 也为空)应 fallback 到拓扑解析(向后兼容)。"""
        job = MagicMock()
        job.job_id = 70004
        job.code = "R36LEGACY"
        job.target_user_id = 80004
        job.storage_channel_id = 90004
        job.storage_msg_ids = [10004]
        job.batch_file_meta = ""
        job.task_type = "single"
        job.protect_content = False
        job.retry_count = 0
        # 旧 job: 结构化字段全空
        job.file_unique_id = ""
        job.group_id = 0
        job.media_group_id = ""

        store = MagicMock()
        store.get_delivery_receipts_by_job = AsyncMock(return_value=[])
        store.upsert_delivery_receipt = AsyncMock(return_value=True)
        store.confirm_delivery_receipt = AsyncMock(return_value=True)

        # mock 拓扑解析返回频道
        fake_resolved = MagicMock()
        fake_resolved.channel_id = 99001
        with patch("bots.dsp_bot._get_store_safe", return_value=store), \
             patch("bots.dsp_bot.resolve_delivery_channel",
                   AsyncMock(return_value=fake_resolved)) as mock_topology, \
             patch("bots.dsp_bot.try_deliver",
                   AsyncMock(return_value=88888)) as mock_deliver, \
             patch("bots.dsp_bot._should_preserve_caption",
                   AsyncMock(return_value=False)), \
             patch("bots.dsp_bot._build_delivery_caption",
                   AsyncMock(return_value="caption")), \
             patch("bots.dsp_bot._edit_sent_caption",
                   AsyncMock(return_value=None)), \
             patch("bots.dsp_bot.metrics") as mock_metrics:
            mock_metrics.record_send_success = AsyncMock()
            mock_metrics.record_processed = AsyncMock()
            result = await dsp_bot_module._process_single_job(
                MagicMock(), job, bot_id=1
            )
        # 应返回 True(拓扑 fallback 成功)
        assert result is True
        # 应调用拓扑解析
        mock_topology.assert_awaited_once()
        # try_deliver 应使用拓扑解析返回的频道
        mock_deliver.assert_awaited_once()


# ════════════════════════════════════════════════════════════════
# 7. 端到端透传测试:cache_store → JobResult → dsp_bot
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestEndToEndPropagation:
    """R36 B0-1: 从 SQLite 写入到 dsp_bot 读取的端到端透传。"""

    @pytest.mark.asyncio
    async def test_full_propagation_from_insert_to_jobresult(self, real_store):
        """端到端: insert_local_job(含新字段) → get_local_pending_jobs →
        _raw_jobs_to_results → JobResult 携带新字段。"""
        # 1. 写入 job(模拟 enqueue_job 内部调用 insert_local_job)
        local_id = await real_store.insert_local_job({
            "code": "R36E2E1",
            "target_user_id": 60001,
            "storage_channel_id": 70001,
            "storage_msg_ids": "[6001]",
            "batch_file_meta": "[]",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            "group_id": 3,
            "file_unique_id": "fuid-e2e-001",
            "media_group_id": "mgid-e2e-001",
        })

        # 2. 读取 pending jobs(模拟 dsp_worker 消费)
        raw_jobs = await real_store.get_local_pending_jobs(limit=10)
        r36_job = next(j for j in raw_jobs if j["code"] == "R36E2E1")
        assert r36_job["group_id"] == 3
        assert r36_job["file_unique_id"] == "fuid-e2e-001"
        assert r36_job["media_group_id"] == "mgid-e2e-001"

        # 3. 转换为 JobResult(模拟 _raw_jobs_to_results)
        results = dsp_bot_module._raw_jobs_to_results([r36_job])
        jr = results[0]
        assert jr.group_id == 3
        assert jr.file_unique_id == "fuid-e2e-001"
        assert jr.media_group_id == "mgid-e2e-001"

        # 4. _extract_replica_info 应优先从结构化字段读取
        fuid, gid, mgid, is_new = dsp_bot_module._extract_replica_info(jr)
        assert fuid == "fuid-e2e-001"
        assert gid == 3
        assert mgid == "mgid-e2e-001"
        assert is_new is True

    @pytest.mark.asyncio
    async def test_get_local_job_by_crdb_id_with_replica_info(self, real_store):
        """按 crdb_id 查询也返回新字段(Redis Stream 消费路径)。"""
        local_id = await real_store.insert_local_job({
            "code": "R36E2E2",
            "target_user_id": 60002,
            "storage_channel_id": 70002,
            "storage_msg_ids": "[6002]",
            "batch_file_meta": "",
            "task_type": "single",
            "status": "pending",
            "protect_content": False,
            "created_at": "2024-01-01T00:00:00Z",
            "group_id": 1,
            "file_unique_id": "fuid-e2e-002",
            "media_group_id": "",
        })
        job_dict = await real_store.get_local_job_by_crdb_id(local_id)
        assert job_dict["group_id"] == 1
        assert job_dict["file_unique_id"] == "fuid-e2e-002"
        assert job_dict["media_group_id"] == ""
        # 转换为 JobResult 后字段应保留
        results = dsp_bot_module._raw_jobs_to_results([job_dict])
        jr = results[0]
        assert jr.group_id == 1
        assert jr.file_unique_id == "fuid-e2e-002"
