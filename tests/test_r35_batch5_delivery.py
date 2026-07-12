"""R35 Batch 5: P0-4 Delivery 接线测试。

被测目标:
- ``bots.dsp_bot`` 的 delivery receipts 双写辅助函数:
  - ``_upsert_delivery_receipt_safe`` — 异常安全写入 PENDING receipt
  - ``_confirm_delivery_receipt_safe`` — 异常安全确认 CONFIRMED
  - ``_mark_delivery_failed_safe`` — 异常安全标记 FAILED
  - ``_get_store_safe`` — 失败时返回 None
- ``_extract_replica_info`` — 从 job.batch_file_meta 提取 (file_unique_id, group_id)
- ``_try_replica_aware_resolve`` — ReplicaAwareResolver 接入(fail-closed fallback)
- ``_send_one_job`` 重试过滤读取持久化 receipts(集成测试)

测试策略:
- 使用真实 SQLite 临时文件数据库,通过 monkeypatch 替换
  ``database.cache_store._store`` 指向临时 CacheStore 实例。
- 验证 delivery_receipts 表完整状态机:
  PENDING(投递前) → CONFIRMED(成功) / FAILED(失败)
- 验证 ReplicaAwareResolver 在缺少 file_unique_id/group_id 时返回 None(fallback)
- 若 bots.dsp_bot 因依赖缺失无法导入,相关测试优雅跳过。

对应 R35 第 21-22 节要求:
- 21.2: 发送每个 source_msg 前查唯一键 + Telegram 成功后立即持久化 sent_msg_id
- 22: Replica-aware 投递真正接线,Manifest 查询失败时 fallback 到拓扑解析
"""
import inspect
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
    """创建一个使用临时文件数据库的 CacheStore 实例。

    隔离策略与 test_r35_batch4_upload.py 一致:
    1. 临时目录下的 test_cache.db(避免污染生产 cache_store.db)。
    2. monkeypatch 替换 ``database.cache_store.DB_PATH`` 模块属性。
    3. 结束后 close + shutil.rmtree。
    """
    tmpdir = tempfile.mkdtemp(prefix="r35_batch5_test_")
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
# 1. delivery_receipts CRUD 基础测试(无需 dsp_bot)
# ════════════════════════════════════════════════════════════════

class TestDeliveryReceiptsCRUD:
    """R35 §21.2: delivery_receipts 表 CRUD 接口验证。"""

    @pytest.mark.asyncio
    async def test_upsert_pending_receipt(self, real_store):
        """写入 PENDING receipt 后能查询到。"""
        await real_store.upsert_delivery_receipt(
            job_id=1001, source_msg_id=2001, target_user_id=3001,
            status="PENDING",
        )
        receipts = await real_store.get_delivery_receipts_by_job(1001)
        assert len(receipts) == 1
        r = receipts[0]
        assert r["job_id"] == 1001
        assert r["source_msg_id"] == 2001
        assert r["target_user_id"] == 3001
        assert r["status"] == "PENDING"
        assert r["sent_msg_id"] is None

    @pytest.mark.asyncio
    async def test_confirm_receipt_transitions_to_confirmed(self, real_store):
        """PENDING → CONFIRMED 状态转换,并写入 sent_msg_id。"""
        await real_store.upsert_delivery_receipt(
            job_id=1002, source_msg_id=2002, target_user_id=3002,
            status="PENDING",
        )
        ok = await real_store.confirm_delivery_receipt(
            job_id=1002, source_msg_id=2002, sent_msg_id=9999,
        )
        assert ok is True
        receipts = await real_store.get_delivery_receipts_by_job(1002)
        assert receipts[0]["status"] == "CONFIRMED"
        assert receipts[0]["sent_msg_id"] == 9999
        assert receipts[0]["confirmed_at"] is not None

    @pytest.mark.asyncio
    async def test_mark_failed_transitions_to_failed(self, real_store):
        """PENDING → FAILED 状态转换,attempts+1, error_reason 记录。"""
        await real_store.upsert_delivery_receipt(
            job_id=1003, source_msg_id=2003, target_user_id=3003,
            status="PENDING",
        )
        ok = await real_store.mark_delivery_failed(
            job_id=1003, source_msg_id=2003, reason="channel_unavailable",
        )
        assert ok is True
        receipts = await real_store.get_delivery_receipts_by_job(1003)
        assert receipts[0]["status"] == "FAILED"
        assert receipts[0]["error_reason"] == "channel_unavailable"
        assert receipts[0]["attempts"] >= 1

    @pytest.mark.asyncio
    async def test_get_sent_msg_ids_filters_status(self, real_store):
        """get_sent_msg_ids_for_job 只返回 SENT/CONFIRMED 状态。"""
        # msg1 = PENDING(不应返回)
        await real_store.upsert_delivery_receipt(
            1004, 2001, 3004, status="PENDING"
        )
        # msg2 = CONFIRMED(应返回)
        await real_store.upsert_delivery_receipt(
            1004, 2002, 3004, sent_msg_id=8001, status="CONFIRMED"
        )
        # msg3 = FAILED(不应返回)
        await real_store.upsert_delivery_receipt(
            1004, 2003, 3004, status="FAILED"
        )
        ids = await real_store.get_sent_msg_ids_for_job(1004)
        assert ids == [8001]

    @pytest.mark.asyncio
    async def test_upsert_is_idempotent_on_unique_key(self, real_store):
        """相同 (job_id, source_msg_id) 重复 upsert 不创建重复记录。"""
        await real_store.upsert_delivery_receipt(
            1005, 2001, 3005, status="PENDING"
        )
        await real_store.upsert_delivery_receipt(
            1005, 2001, 3005, sent_msg_id=9001, status="CONFIRMED"
        )
        receipts = await real_store.get_delivery_receipts_by_job(1005)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "CONFIRMED"
        assert receipts[0]["sent_msg_id"] == 9001


# ════════════════════════════════════════════════════════════════
# 2. dsp_bot 辅助函数测试(需要导入 dsp_bot)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用(需要 telegram / loguru 等依赖)",
)
class TestDspBotDeliveryReceiptsHelpers:
    """R35 §21.2: dsp_bot 的 delivery_receipts 双写辅助函数。"""

    @pytest.mark.asyncio
    async def test_upsert_safe_with_none_store(self):
        """store=None 时不抛异常,静默返回。"""
        await dsp_bot_module._upsert_delivery_receipt_safe(
            None, job_id=1, source_msg_id=1, target_user_id=1, status="PENDING"
        )

    @pytest.mark.asyncio
    async def test_confirm_safe_with_none_store(self):
        """store=None 时不抛异常。"""
        await dsp_bot_module._confirm_delivery_receipt_safe(
            None, job_id=1, source_msg_id=1, sent_msg_id=1
        )

    @pytest.mark.asyncio
    async def test_mark_failed_safe_with_none_store(self):
        """store=None 时不抛异常。"""
        await dsp_bot_module._mark_delivery_failed_safe(
            None, job_id=1, source_msg_id=1, reason="test"
        )

    @pytest.mark.asyncio
    async def test_upsert_safe_with_failing_store(self):
        """store 方法抛异常时被捕获,不传播到主流程(只 warning)。"""
        failing_store = MagicMock()
        failing_store.upsert_delivery_receipt = AsyncMock(
            side_effect=RuntimeError("db locked")
        )
        # 不应抛异常
        await dsp_bot_module._upsert_delivery_receipt_safe(
            failing_store, job_id=1, source_msg_id=1, target_user_id=1,
            status="PENDING",
        )
        failing_store.upsert_delivery_receipt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirm_safe_with_failing_store(self):
        """confirm 抛异常时被捕获。"""
        failing_store = MagicMock()
        failing_store.confirm_delivery_receipt = AsyncMock(
            side_effect=RuntimeError("db error")
        )
        await dsp_bot_module._confirm_delivery_receipt_safe(
            failing_store, job_id=1, source_msg_id=1, sent_msg_id=1
        )
        failing_store.confirm_delivery_receipt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_failed_safe_with_failing_store(self):
        """mark_failed 抛异常时被捕获。"""
        failing_store = MagicMock()
        failing_store.mark_delivery_failed = AsyncMock(
            side_effect=RuntimeError("db error")
        )
        await dsp_bot_module._mark_delivery_failed_safe(
            failing_store, job_id=1, source_msg_id=1, reason="test"
        )
        failing_store.mark_delivery_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_safe_writes_to_real_store(self, real_store):
        """辅助函数成功写入真实 SQLite 库。"""
        await dsp_bot_module._upsert_delivery_receipt_safe(
            real_store, job_id=2001, source_msg_id=3001,
            target_user_id=4001, status="PENDING",
        )
        receipts = await real_store.get_delivery_receipts_by_job(2001)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "PENDING"

    @pytest.mark.asyncio
    async def test_full_pending_to_confirmed_flow(self, real_store):
        """完整流程: PENDING → CONFIRMED 通过辅助函数实现。"""
        jid, mid, uid, sent = 2002, 3002, 4002, 9999
        await dsp_bot_module._upsert_delivery_receipt_safe(
            real_store, jid, mid, uid, status="PENDING"
        )
        await dsp_bot_module._confirm_delivery_receipt_safe(
            real_store, jid, mid, sent
        )
        receipts = await real_store.get_delivery_receipts_by_job(jid)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "CONFIRMED"
        assert receipts[0]["sent_msg_id"] == sent

    @pytest.mark.asyncio
    async def test_full_pending_to_failed_flow(self, real_store):
        """完整流程: PENDING → FAILED 通过辅助函数实现。"""
        jid, mid, uid = 2003, 3003, 4003
        await dsp_bot_module._upsert_delivery_receipt_safe(
            real_store, jid, mid, uid, status="PENDING"
        )
        await dsp_bot_module._mark_delivery_failed_safe(
            real_store, jid, mid, reason="all_channels_unavailable"
        )
        receipts = await real_store.get_delivery_receipts_by_job(jid)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "FAILED"
        assert receipts[0]["error_reason"] == "all_channels_unavailable"


# ════════════════════════════════════════════════════════════════
# 3. _extract_replica_info 测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestExtractReplicaInfo:
    """R35 §22: 从 job.batch_file_meta 提取 (file_unique_id, group_id)。"""

    def test_empty_meta_returns_empty_tuple(self):
        """空 batch_file_meta 返回 ("", None)。"""
        job = MagicMock()
        job.batch_file_meta = ""
        fuid, gid = dsp_bot_module._extract_replica_info(job)
        assert fuid == ""
        assert gid is None

    def test_none_meta_returns_empty_tuple(self):
        """None batch_file_meta 返回 ("", None)。"""
        job = MagicMock()
        job.batch_file_meta = None
        fuid, gid = dsp_bot_module._extract_replica_info(job)
        assert fuid == ""
        assert gid is None

    def test_json_string_with_file_unique_id(self):
        """JSON 字符串格式提取 file_unique_id。"""
        job = MagicMock()
        job.batch_file_meta = (
            '[{"chat_id": 1, "msg_id": 100, "file_unique_id": "fuid-abc-001", '
            '"file_type": "photo"}]'
        )
        fuid, gid = dsp_bot_module._extract_replica_info(job)
        assert fuid == "fuid-abc-001"
        # group_id 当前数据流未暴露
        assert gid is None

    def test_list_format_with_file_unique_id(self):
        """list 格式提取 file_unique_id。"""
        job = MagicMock()
        job.batch_file_meta = [
            {"chat_id": 1, "msg_id": 100, "file_unique_id": "fuid-list-001"},
        ]
        fuid, gid = dsp_bot_module._extract_replica_info(job)
        assert fuid == "fuid-list-001"
        assert gid is None

    def test_invalid_json_returns_empty(self):
        """无效 JSON 返回空。"""
        job = MagicMock()
        job.batch_file_meta = "not a json"
        fuid, gid = dsp_bot_module._extract_replica_info(job)
        assert fuid == ""
        assert gid is None

    def test_first_item_missing_file_unique_id(self):
        """首条记录无 file_unique_id 字段时返回空字符串。"""
        job = MagicMock()
        job.batch_file_meta = [{"chat_id": 1, "msg_id": 100}]
        fuid, gid = dsp_bot_module._extract_replica_info(job)
        assert fuid == ""
        assert gid is None


# ════════════════════════════════════════════════════════════════
# 4. _try_replica_aware_resolve 测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestTryReplicaAwareResolve:
    """R35 §22: ReplicaAwareResolver 接入测试。"""

    @pytest.mark.asyncio
    async def test_empty_file_unique_id_returns_none(self):
        """file_unique_id 为空时返回 None(fallback 到拓扑解析)。"""
        result = await dsp_bot_module._try_replica_aware_resolve(
            store=MagicMock(), file_unique_id="", group_id=1,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_group_id_returns_none(self):
        """group_id 为 None 时返回 None(当前数据流常态)。"""
        result = await dsp_bot_module._try_replica_aware_resolve(
            store=MagicMock(), file_unique_id="fuid-1", group_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_store_returns_none(self):
        """store 为 None 时返回 None。"""
        result = await dsp_bot_module._try_replica_aware_resolve(
            store=None, file_unique_id="fuid-1", group_id=1,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_resolver_exception_returns_none(self):
        """ReplicaAwareResolver 抛异常时返回 None(fail-closed)。"""
        fake_store = MagicMock()
        # 让 ReplicaAwareResolver 构造或调用时抛异常
        with patch(
            "services.delivery_resolver.ReplicaAwareResolver"
        ) as mock_cls:
            mock_instance = MagicMock()
            mock_instance.resolve_channel_for_file = AsyncMock(
                side_effect=RuntimeError("manifest query failed")
            )
            mock_cls.return_value = mock_instance
            result = await dsp_bot_module._try_replica_aware_resolve(
                store=fake_store, file_unique_id="fuid-1", group_id=1,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_resolver_returns_valid_channel(self):
        """ReplicaAwareResolver 命中时返回 (channel_id, message_id)。"""
        fake_store = MagicMock()
        with patch(
            "services.delivery_resolver.ReplicaAwareResolver"
        ) as mock_cls:
            mock_instance = MagicMock()
            mock_instance.resolve_channel_for_file = AsyncMock(
                return_value=(12345, 67890)
            )
            mock_cls.return_value = mock_instance
            result = await dsp_bot_module._try_replica_aware_resolve(
                store=fake_store, file_unique_id="fuid-1", group_id=1,
                preferred_channels=[100],
            )
        assert result == (12345, 67890)
        # 验证 preferred_channels 被传递
        mock_instance.resolve_channel_for_file.assert_awaited_once()
        call_kwargs = mock_instance.resolve_channel_for_file.await_args.kwargs
        assert call_kwargs["preferred_channels"] == [100]


# ════════════════════════════════════════════════════════════════
# 5. _get_store_safe 测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestGetStoreSafe:
    """R35 §21.2: _get_store_safe 安全获取 cache_store 实例。"""

    def test_returns_store_when_available(self):
        """cache_store 可用时返回实例。"""
        fake_store = MagicMock()
        with patch("database.cache_store.get_cache_store", return_value=fake_store):
            result = dsp_bot_module._get_store_safe()
        assert result is fake_store

    def test_returns_none_when_import_fails(self):
        """导入失败时返回 None,不抛异常。"""
        with patch(
            "database.cache_store.get_cache_store",
            side_effect=ImportError("aiosqlite missing"),
        ):
            result = dsp_bot_module._get_store_safe()
        assert result is None


# ════════════════════════════════════════════════════════════════
# 6. _send_one_job 重试过滤集成测试(读取持久化 receipts)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestSendOneJobRetryFilter:
    """R35 §21.2: _send_one_job 重试过滤读取持久化 receipts。"""

    @pytest.mark.asyncio
    async def test_retry_skips_already_delivered_msg_ids(self, real_store, monkeypatch):
        """已持久化 SENT/CONFIRMED 的 msg_id 在重试时被跳过。"""
        # 准备 job 数据
        job = MagicMock()
        job.job_id = 5001
        job.code = "TESTCODE"
        job.target_user_id = 6001
        job.storage_channel_id = 7001
        job.storage_msg_ids = [100, 101, 102]
        job.batch_file_meta = ""
        job.task_type = "single"
        job.protect_content = False
        job.retry_count = 1

        # 预置持久化 receipts:msg_id=100 已 CONFIRMED
        await real_store.upsert_delivery_receipt(
            5001, 100, 6001, sent_msg_id=9001, status="CONFIRMED"
        )
        # msg_id=101 已 SENT
        await real_store.upsert_delivery_receipt(
            5001, 101, 6001, sent_msg_id=9002, status="SENT"
        )
        # msg_id=102 仍 PENDING(需重试)

        # mock store:返回真实 store 行为 + 必要方法
        store = MagicMock()
        store.get_sent_msg_ids_for_job = real_store.get_sent_msg_ids_for_job
        store.get_delivery_receipts_by_job = real_store.get_delivery_receipts_by_job
        store.is_user_started = AsyncMock(return_value=True)
        store.update_local_job_status = AsyncMock()
        store.mark_local_job_dispatched = AsyncMock(return_value=True)
        store.retry_local_job = AsyncMock()

        # mock dynamic_rate_limiter.acquire 不阻塞
        async def _noop_acquire(*args, **kwargs):
            return None
        monkeypatch.setattr(
            dsp_bot_module.dynamic_rate_limiter, "acquire", _noop_acquire
        )

        # mock get_pending_jobs_count_local
        monkeypatch.setattr(
            dsp_bot_module, "get_pending_jobs_count_local", lambda: 0
        )

        # 让 _send_semaphore 立即获取
        # 已存在模块级 semaphore,直接使用即可

        # mock _process_single_job:验证传入的 storage_msg_ids 已过滤
        captured_msg_ids = []

        async def _capture_process(bot, j, bot_id=1):
            captured_msg_ids.extend(list(j.storage_msg_ids))
            return True

        monkeypatch.setattr(dsp_bot_module, "_process_single_job", _capture_process)
        # mock _send_report_button 避免 telegram 调用
        async def _noop_report(*args, **kwargs):
            return None
        monkeypatch.setattr(dsp_bot_module, "_send_report_button", _noop_report)

        bot = MagicMock()
        await dsp_bot_module._send_one_job(bot, job, worker_id=1, store=store)

        # 验证:只投递了 msg_id=102(100 和 101 已被持久化 receipts 跳过)
        assert captured_msg_ids == [102]

    @pytest.mark.asyncio
    async def test_retry_completes_when_all_already_delivered(self, real_store, monkeypatch):
        """所有 msg_id 都已持久化 SENT/CONFIRMED 时,直接标记 done,不调用投递。"""
        job = MagicMock()
        job.job_id = 5002
        job.code = "TESTCODE2"
        job.target_user_id = 6002
        job.storage_channel_id = 7002
        job.storage_msg_ids = [200, 201]
        job.batch_file_meta = ""
        job.task_type = "single"
        job.protect_content = False
        job.retry_count = 1

        # 预置所有 msg_id 已 CONFIRMED
        await real_store.upsert_delivery_receipt(
            5002, 200, 6002, sent_msg_id=9100, status="CONFIRMED"
        )
        await real_store.upsert_delivery_receipt(
            5002, 201, 6002, sent_msg_id=9101, status="CONFIRMED"
        )

        store = MagicMock()
        store.get_sent_msg_ids_for_job = real_store.get_sent_msg_ids_for_job
        store.is_user_started = AsyncMock(return_value=True)
        store.update_local_job_status = AsyncMock()
        store.mark_local_job_dispatched = AsyncMock(return_value=True)

        async def _noop_acquire(*args, **kwargs):
            return None
        monkeypatch.setattr(
            dsp_bot_module.dynamic_rate_limiter, "acquire", _noop_acquire
        )
        monkeypatch.setattr(
            dsp_bot_module, "get_pending_jobs_count_local", lambda: 0
        )

        process_called = False

        async def _should_not_be_called(bot, j, bot_id=1):
            nonlocal process_called
            process_called = True
            return True

        monkeypatch.setattr(dsp_bot_module, "_process_single_job", _should_not_be_called)

        async def _noop_report(*args, **kwargs):
            return None
        monkeypatch.setattr(dsp_bot_module, "_send_report_button", _noop_report)

        bot = MagicMock()
        result = await dsp_bot_module._send_one_job(bot, job, worker_id=1, store=store)

        # 验证:_process_single_job 未被调用,job 直接标记 done
        assert result is True
        assert not process_called
        store.update_local_job_status.assert_awaited()
        # 验证状态为 done
        call_args = store.update_local_job_status.await_args
        assert call_args.args[1] == "done"


# ════════════════════════════════════════════════════════════════
# 7. _process_single_job 双写集成测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestProcessSingleJobDualWrite:
    """R35 §21.2: _process_single_job 双写 delivery_receipts。"""

    @pytest.mark.asyncio
    async def test_success_writes_confirmed_receipt(self, real_store, monkeypatch):
        """投递成功后 delivery_receipts 表中有 CONFIRMED 记录。"""
        job = MagicMock()
        job.job_id = 7001
        job.code = "DUALTEST"
        job.target_user_id = 8001
        job.storage_channel_id = 9001
        job.storage_msg_ids = [500]
        job.batch_file_meta = ""  # 无 file_unique_id → 走 fallback 路径
        job.protect_content = False

        # mock get_cache_store 返回真实 store
        monkeypatch.setattr(
            "database.cache_store.get_cache_store", lambda: real_store
        )

        # mock resolve_delivery_channel 返回伪 DeliveryChannel
        fake_channel = MagicMock()
        fake_channel.channel_id = 9001
        fake_channel.status = "active"

        async def _fake_resolve(channel_id):
            return fake_channel

        monkeypatch.setattr(
            dsp_bot_module, "resolve_delivery_channel", _fake_resolve
        )

        # mock try_deliver 返回成功 sent_msg_id
        async def _fake_deliver(bot, target, channel, msg_id, **kwargs):
            return 9999

        monkeypatch.setattr(dsp_bot_module, "try_deliver", _fake_deliver)

        # mock caption 相关
        async def _true(*args, **kwargs):
            return True
        monkeypatch.setattr(dsp_bot_module, "_should_preserve_caption", _true)

        bot = MagicMock()
        result = await dsp_bot_module._process_single_job(bot, job, bot_id=1)

        # 验证返回 True
        assert result is True

        # 验证 delivery_receipts 表有 CONFIRMED 记录
        receipts = await real_store.get_delivery_receipts_by_job(7001)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "CONFIRMED"
        assert receipts[0]["sent_msg_id"] == 9999
        assert receipts[0]["source_msg_id"] == 500
        assert receipts[0]["target_user_id"] == 8001

        # 验证 _sent_msg_tracker 也被更新(双写)
        assert 500 in dsp_bot_module._sent_msg_tracker.get(7001, set())
        # 清理
        dsp_bot_module._sent_msg_tracker.pop(7001, None)

    @pytest.mark.asyncio
    async def test_failure_writes_failed_receipt(self, real_store, monkeypatch):
        """投递失败后 delivery_receipts 表中有 FAILED 记录。"""
        job = MagicMock()
        job.job_id = 7002
        job.code = "FAILTEST"
        job.target_user_id = 8002
        job.storage_channel_id = 9002
        job.storage_msg_ids = [501]
        job.batch_file_meta = ""
        job.protect_content = False

        monkeypatch.setattr(
            "database.cache_store.get_cache_store", lambda: real_store
        )

        # mock resolve_delivery_channel
        fake_channel = MagicMock()
        fake_channel.channel_id = 9002
        fake_channel.status = "active"

        async def _fake_resolve(channel_id):
            return fake_channel

        monkeypatch.setattr(
            dsp_bot_module, "resolve_delivery_channel", _fake_resolve
        )

        # mock _walk_ring_for_channel 返回同一 channel(避免无限循环)
        async def _fake_walk(channel_id, max_hops=5):
            return fake_channel

        # 需要修改 storage.delivery_resolver 模块的 _walk_ring_for_channel
        with patch(
            "storage.delivery_resolver._walk_ring_for_channel", _fake_walk
        ):
            # mock try_deliver 始终返回 None(失败)
            async def _fail_deliver(bot, target, channel, msg_id, **kwargs):
                return None

            monkeypatch.setattr(dsp_bot_module, "try_deliver", _fail_deliver)

            # mock _record_channel_failure 避免污染全局状态
            async def _noop_record(*args, **kwargs):
                return None
            monkeypatch.setattr(dsp_bot_module, "_record_channel_failure", _noop_record)

            # mock safe_send_message 避免 telegram 调用
            async def _noop_send(*args, **kwargs):
                return None
            monkeypatch.setattr(dsp_bot_module, "safe_send_message", _noop_send)

            bot = MagicMock()
            result = await dsp_bot_module._process_single_job(bot, job, bot_id=1)

        # 验证返回 False
        assert result is False

        # 验证 delivery_receipts 表有 FAILED 记录
        receipts = await real_store.get_delivery_receipts_by_job(7002)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "FAILED"
        assert receipts[0]["source_msg_id"] == 501
        assert receipts[0]["error_reason"] is not None
        assert "all_channels_unavailable" in receipts[0]["error_reason"]

        # 清理
        dsp_bot_module._sent_msg_tracker.pop(7002, None)


# ════════════════════════════════════════════════════════════════
# 8. _sent_msg_tracker 内存缓存保留验证
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _dsp_bot_available,
    reason="bots.dsp_bot 不可用",
)
class TestSentMsgTrackerPreserved:
    """R35 §21.2: _sent_msg_tracker 作为内存缓存层保留(向后兼容)。"""

    def test_sent_msg_tracker_dict_exists(self):
        """_sent_msg_tracker 仍作为模块级变量存在。"""
        assert hasattr(dsp_bot_module, "_sent_msg_tracker")
        assert isinstance(dsp_bot_module._sent_msg_tracker, dict)

    def test_sent_msg_tracker_setdefault_works(self):
        """_sent_msg_tracker 支持 setdefault 接口(向后兼容)。"""
        # 清理可能存在的测试数据
        dsp_bot_module._sent_msg_tracker.pop(99990, None)
        try:
            s = dsp_bot_module._sent_msg_tracker.setdefault(99990, set())
            s.add(12345)
            assert 12345 in dsp_bot_module._sent_msg_tracker[99990]
        finally:
            dsp_bot_module._sent_msg_tracker.pop(99990, None)
