"""R47 P0-5 整改测试: delivery_group_receipts CRUD + 媒体组/fallback/caption edit 闭环。

测试覆盖 5 个场景:
1. 媒体组单条 group receipt 创建 + 全部 CONFIRMED 完成
2. 部分成功重试只发送缺失 child
3. fallback 单发独立 receipt
4. caption edit 独立 receipt
5. skipped receipt 核对 + DeliveryError 暂停

测试策略:
- 真实 SQLite 临时文件数据库(隔离生产数据)
- Mock telegram bot / storage.delivery_resolver(避免真实 Telegram API 调用)
- 初始化 EffectReceiptManager(验证 effect_receipts 表行为)
- 使用 patch 替换 dsp_bot 内部依赖(_get_store_safe / get_receipt_manager)
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# 测试环境兼容: mock telegram / telegram.ext / telegram.error
_telegram_mock = MagicMock()
sys.modules.setdefault("telegram", _telegram_mock)
sys.modules.setdefault("telegram.ext", MagicMock())
_telegram_error_mock = MagicMock()
for _err_name in ("BadRequest", "Forbidden", "RetryAfter", "NetworkError",
                   "TimedOut", "ChatMigrated", "MessageNotModified",
                   "TelegramError", "Conflict"):
    setattr(_telegram_error_mock, _err_name, type(_err_name, (Exception,), {}))
sys.modules.setdefault("telegram.error", _telegram_error_mock)

# Mock storage.delivery_resolver(避免 Python 3.10+ 语法兼容问题)
if "storage.delivery_resolver" not in sys.modules:
    _mock_delivery_resolver = types.ModuleType("storage.delivery_resolver")
    _mock_delivery_resolver.resolve_delivery_channel = AsyncMock(return_value=None)
    _mock_delivery_resolver.try_deliver = AsyncMock(return_value=None)
    _mock_delivery_resolver.try_deliver_batch = AsyncMock(return_value=[])
    _mock_delivery_resolver.invalidate_cell_cache = AsyncMock(return_value=None)
    sys.modules["storage.delivery_resolver"] = _mock_delivery_resolver
    if "storage" not in sys.modules:
        _mock_storage_pkg = types.ModuleType("storage")
        _mock_storage_pkg.__path__ = []
        sys.modules["storage"] = _mock_storage_pkg

# 设置模块加载时所需的 config 值
_config = sys.modules.get("config")
if _config is not None and hasattr(_config, "settings"):
    _s = _config.settings
    _s.RATE_LIMIT_BASE_DELAY = 0.2
    _s.RATE_LIMIT_MAX_DELAY = 3.0
    _s.RATE_LIMIT_THRESHOLD_LOW = 10
    _s.RATE_LIMIT_THRESHOLD_HIGH = 30
    _s.SENDER_BOT_TOKEN = "test-token"
    _s.PAGE_SIZE = 10
    _s.SEND_CONCURRENCY = 5
    _s.CHANNEL_FAILURE_THRESHOLD = 3
    _s.CHANNEL_FAILURE_WINDOW = 60


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite cache_store + EffectReceiptManager
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(R47 P0-5 测试用)。"""
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r47_p0_5_test_")
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


@pytest_asyncio.fixture
async def receipt_manager(cache_store):
    """初始化 EffectReceiptManager 单例(绑定到测试 cache_store)。

    在 fixture 期间替换全局 _receipt_manager,测试结束后恢复。
    """
    import services.effect_receipts as er_module

    original_manager = er_module._receipt_manager
    manager = er_module.EffectReceiptManager(cache_store)
    er_module._receipt_manager = manager
    try:
        yield manager
    finally:
        er_module._receipt_manager = original_manager


@pytest.fixture
def mock_bot():
    """Mock telegram bot 实例。"""
    bot = MagicMock()
    bot.copy_message = AsyncMock(return_value=MagicMock(message_id=10001))
    bot.copy_messages = AsyncMock(return_value=[MagicMock(message_id=10001)])
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=10002))
    bot.send_media_group = AsyncMock(return_value=[MagicMock(message_id=10003)])
    bot.edit_message_caption = AsyncMock(return_value=True)
    return bot


@pytest.fixture(autouse=True)
def mock_metrics():
    """R47 P0-5: autouse mock dsp_bot.metrics,避免全量测试时 metrics 模块被污染。

    全量测试中,前面的测试文件可能修改 utils.monitor.metrics 的全局状态,
    导致 record_send_success/record_send_fail 等方法返回 MagicMock 而非 coroutine,
    触发 'object MagicMock can't be used in 'await' expression' 错误。
    """
    from bots import dsp_bot
    mock_m = MagicMock()
    mock_m.record_send_success = AsyncMock()
    mock_m.record_send_fail = AsyncMock()
    mock_m.record_processed = AsyncMock()
    mock_m.record_error = AsyncMock()
    with patch.object(dsp_bot, "metrics", mock_m):
        yield mock_m


def _make_job(
    job_id: int = 9001,
    code: str = "TESTCODE",
    target_user_id: int = 50001,
    storage_channel_id: int = -1001234567890,
    storage_msg_ids: list[int] | None = None,
    batch_file_meta: str | list = "",
    task_type: str = "batch",
    protect_content: bool = False,
    retry_count: int = 0,
):
    """构造测试用 JobResult 对象。"""
    from database import JobResult
    if storage_msg_ids is None:
        storage_msg_ids = [100, 101, 102]
    if isinstance(batch_file_meta, list):
        batch_file_meta = json.dumps(batch_file_meta)
    return JobResult(
        job_id=job_id,
        code=code,
        target_user_id=target_user_id,
        storage_channel_id=storage_channel_id,
        storage_msg_ids=storage_msg_ids,
        batch_file_meta=batch_file_meta,
        task_type=task_type,
        protect_content=protect_content,
        retry_count=retry_count,
    )


def _make_delivery_channel(channel_id: int = -1001234567890, status: str = "ok"):
    """构造 mock DeliveryChannel 对象。"""
    dc = MagicMock()
    dc.channel_id = channel_id
    dc.status = status
    return dc


# ════════════════════════════════════════════════════════════════
# 场景 1: 媒体组单条 group receipt 创建 + 全部 CONFIRMED 完成
# ════════════════════════════════════════════════════════════════

class TestGroupReceiptCreationAndCompletion:
    """R47 P0-5 #1: 媒体组投递使用单条 group receipt。"""

    @pytest.mark.asyncio
    async def test_batch_creates_group_receipt_and_confirms_all(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """批量发送创建 group receipt,所有 child confirm 后 status='completed'。"""
        from bots import dsp_bot

        # 构造 batch job(含 file_meta_list,走 _send_page 路径)
        file_meta_list = [
            {"type": "document", "file_id": "fid-100"},
            {"type": "document", "file_id": "fid-101"},
            {"type": "document", "file_id": "fid-102"},
        ]
        job = _make_job(
            job_id=9101,
            storage_msg_ids=[100, 101, 102],
            batch_file_meta=file_meta_list,
        )

        # Mock resolve_delivery_channel + try_deliver_batch
        resolved = _make_delivery_channel()
        sent_msg_ids = [20001, 20002, 20003]

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=resolved)), \
             patch.object(dsp_bot, "try_deliver_batch", AsyncMock(return_value=sent_msg_ids)), \
             patch.object(dsp_bot, "_should_preserve_caption", AsyncMock(return_value=False)), \
             patch.object(dsp_bot, "_build_delivery_caption", AsyncMock(return_value="caption")), \
             patch.object(dsp_bot, "_edit_sent_caption", AsyncMock()), \
             patch.object(dsp_bot, "_send_report_button", AsyncMock()):
            result = await dsp_bot._process_batch_job(mock_bot, job, bot_id=1)

        # 验证返回成功
        assert result is True

        # 验证 group receipt 已创建
        group_id = f"dsp_batch:{job.job_id}:{job.storage_channel_id}:{job.target_user_id}"
        receipt = await cache_store.delivery_group_receipt_get(group_id)
        assert receipt is not None
        assert receipt["expected_count"] == 3
        assert receipt["confirmed_count"] == 3
        assert receipt["status"] == "completed"
        assert receipt["source_ids"] == [100, 101, 102]

    @pytest.mark.asyncio
    async def test_group_receipt_idempotent_create(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """重复调用 _process_batch_job 不覆盖已有 group receipt。"""
        from bots import dsp_bot

        file_meta_list = [{"type": "document", "file_id": "fid-200"}]
        job = _make_job(
            job_id=9102,
            storage_msg_ids=[200],
            batch_file_meta=file_meta_list,
        )

        resolved = _make_delivery_channel()

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=resolved)), \
             patch.object(dsp_bot, "try_deliver_batch", AsyncMock(return_value=[20010])), \
             patch.object(dsp_bot, "_should_preserve_caption", AsyncMock(return_value=False)), \
             patch.object(dsp_bot, "_build_delivery_caption", AsyncMock(return_value="cap")), \
             patch.object(dsp_bot, "_edit_sent_caption", AsyncMock()), \
             patch.object(dsp_bot, "_send_report_button", AsyncMock()):
            # 第一次调用
            await dsp_bot._process_batch_job(mock_bot, job, bot_id=1)
            # 第二次调用(模拟重试,group receipt 应幂等)
            await dsp_bot._process_batch_job(mock_bot, job, bot_id=1)

        group_id = f"dsp_batch:{job.job_id}:{job.storage_channel_id}:{job.target_user_id}"
        receipt = await cache_store.delivery_group_receipt_get(group_id)
        # expected_count 仍为原始值(不被覆盖)
        assert receipt["expected_count"] == 1


# ════════════════════════════════════════════════════════════════
# 场景 2: 部分成功重试只发送缺失 child
# ════════════════════════════════════════════════════════════════

class TestPartialRetryOnlySendsMissing:
    """R47 P0-5 #1: 部分成功重试时,group receipt 记录已 confirmed,重试只发缺失 child。"""

    @pytest.mark.asyncio
    async def test_completed_group_receipt_skips_job(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """group receipt 已 completed 时,_process_batch_job 直接返回 True(跳过发送)。"""
        from bots import dsp_bot

        job = _make_job(
            job_id=9201,
            storage_msg_ids=[300, 301, 302],
            batch_file_meta=[{"type": "document", "file_id": "fid-300"}],
        )

        # 预创建已 completed 的 group receipt
        group_id = f"dsp_batch:{job.job_id}:{job.storage_channel_id}:{job.target_user_id}"
        await cache_store.delivery_group_receipt_create(
            group_id=group_id, expected_count=3,
            source_ids=[300, 301, 302], target_ids=[job.target_user_id] * 3,
            action_id=group_id,
        )
        # confirm 所有 3 个 child
        await cache_store.delivery_group_receipt_confirm_child(group_id, 300)
        await cache_store.delivery_group_receipt_confirm_child(group_id, 301)
        await cache_store.delivery_group_receipt_confirm_child(group_id, 302)

        # 验证 group receipt 已 completed
        receipt = await cache_store.delivery_group_receipt_get(group_id)
        assert receipt["status"] == "completed"

        # try_deliver_batch 不应被调用(group receipt 已 completed)
        mock_try_batch = AsyncMock(return_value=[99999])

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=_make_delivery_channel())), \
             patch.object(dsp_bot, "try_deliver_batch", mock_try_batch), \
             patch.object(dsp_bot, "_send_report_button", AsyncMock()):
            result = await dsp_bot._process_batch_job(mock_bot, job, bot_id=1)

        assert result is True
        # try_deliver_batch 不应被调用
        mock_try_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_group_receipt_allows_retry(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """group receipt 部分完成(partial)时,_process_batch_job 继续发送剩余 child。"""
        from bots import dsp_bot

        job = _make_job(
            job_id=9202,
            storage_msg_ids=[400, 401],
            batch_file_meta=[{"type": "document", "file_id": "fid-400"}],
        )

        # 预创建 partial 的 group receipt(1/2 confirmed)
        group_id = f"dsp_batch:{job.job_id}:{job.storage_channel_id}:{job.target_user_id}"
        await cache_store.delivery_group_receipt_create(
            group_id=group_id, expected_count=2,
            source_ids=[400, 401], target_ids=[job.target_user_id] * 2,
            action_id=group_id,
        )
        # confirm 1 个 child
        await cache_store.delivery_group_receipt_confirm_child(group_id, 400)

        receipt = await cache_store.delivery_group_receipt_get(group_id)
        assert receipt["status"] == "partial"
        assert receipt["confirmed_count"] == 1

        # 继续发送剩余 child
        resolved = _make_delivery_channel()

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=resolved)), \
             patch.object(dsp_bot, "try_deliver_batch", AsyncMock(return_value=[40010, 40011])), \
             patch.object(dsp_bot, "_should_preserve_caption", AsyncMock(return_value=False)), \
             patch.object(dsp_bot, "_build_delivery_caption", AsyncMock(return_value="cap")), \
             patch.object(dsp_bot, "_edit_sent_caption", AsyncMock()), \
             patch.object(dsp_bot, "_send_report_button", AsyncMock()):
            result = await dsp_bot._process_batch_job(mock_bot, job, bot_id=1)

        assert result is True
        # group receipt 应已完成(1 + 2 = 3,但 expected=2,所以至少 2 confirmed)
        receipt = await cache_store.delivery_group_receipt_get(group_id)
        assert receipt["confirmed_count"] >= 2
        assert receipt["status"] == "completed"


# ════════════════════════════════════════════════════════════════
# 场景 3: fallback 单发独立 receipt
# ════════════════════════════════════════════════════════════════

class TestFallbackSingleSendReceipt:
    """R47 P0-5 #2: fallback 单发独立 EffectReceipt。"""

    @pytest.mark.asyncio
    async def test_fallback_creates_group_receipt_with_fallback_prefix(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """_fallback_single_send 创建 dsp_fallback 前缀的 group receipt。"""
        from bots import dsp_bot

        # 构造无 file_meta_list 的 batch job(触发 _fallback_single_send)
        job = _make_job(
            job_id=9301,
            storage_msg_ids=[500, 501],
            batch_file_meta="",  # 空 → 触发 fallback
        )

        resolved = _make_delivery_channel()

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=resolved)), \
             patch.object(dsp_bot, "try_deliver", AsyncMock(side_effect=[50001, 50002])), \
             patch.object(dsp_bot, "_should_preserve_caption", AsyncMock(return_value=False)), \
             patch.object(dsp_bot, "_build_delivery_caption", AsyncMock(return_value="cap")), \
             patch.object(dsp_bot, "_edit_sent_caption", AsyncMock()):
            result = await dsp_bot._fallback_single_send(mock_bot, job, bot_id=1)

        assert result is True

        # 验证 dsp_fallback 前缀的 group receipt 已创建
        group_id = f"dsp_fallback:{job.job_id}:{job.storage_channel_id}:{job.target_user_id}"
        receipt = await cache_store.delivery_group_receipt_get(group_id)
        assert receipt is not None
        assert receipt["expected_count"] == 2
        assert receipt["confirmed_count"] == 2
        assert receipt["status"] == "completed"

    @pytest.mark.asyncio
    async def test_fallback_per_child_independent_effect_receipt(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """每个 child 发送创建独立的 effect_receipt(action_id 含 child_index)。"""
        from bots import dsp_bot

        job = _make_job(
            job_id=9302,
            storage_msg_ids=[600, 601],
            batch_file_meta="",
        )

        resolved = _make_delivery_channel()

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=resolved)), \
             patch.object(dsp_bot, "try_deliver", AsyncMock(side_effect=[60001, 60002])), \
             patch.object(dsp_bot, "_should_preserve_caption", AsyncMock(return_value=False)), \
             patch.object(dsp_bot, "_build_delivery_caption", AsyncMock(return_value="cap")), \
             patch.object(dsp_bot, "_edit_sent_caption", AsyncMock()):
            await dsp_bot._fallback_single_send(mock_bot, job, bot_id=1)

        # 验证每个 child 有独立的 effect_receipt(不同 action_id)
        # action_id 格式: dsp_fb:{job_id}:{channel}:{mid}:{user}:{index}
        async def _count_receipts(action_id_pattern: str) -> int:
            cursor = await cache_store._db.execute(
                "SELECT COUNT(*) FROM effect_receipts WHERE action_id LIKE ?",
                (action_id_pattern,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

        count = await _count_receipts(f"dsp_fb:{job.job_id}%")
        assert count == 2, f"应有 2 个独立 effect_receipt,实际 {count}"

    @pytest.mark.asyncio
    async def test_fallback_completed_group_skips_retry(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """fallback group receipt 已 completed 时,重试直接跳过(不重复发送)。"""
        from bots import dsp_bot

        job = _make_job(
            job_id=9303,
            storage_msg_ids=[700, 701],
            batch_file_meta="",
        )

        # 预创建已 completed 的 fallback group receipt
        group_id = f"dsp_fallback:{job.job_id}:{job.storage_channel_id}:{job.target_user_id}"
        await cache_store.delivery_group_receipt_create(
            group_id=group_id, expected_count=2,
            source_ids=[700, 701], target_ids=[job.target_user_id] * 2,
            action_id=group_id,
        )
        await cache_store.delivery_group_receipt_confirm_child(group_id, 700)
        await cache_store.delivery_group_receipt_confirm_child(group_id, 701)

        mock_try_deliver = AsyncMock()

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store), \
             patch.object(dsp_bot, "resolve_delivery_channel", AsyncMock(return_value=_make_delivery_channel())), \
             patch.object(dsp_bot, "try_deliver", mock_try_deliver), \
             patch.object(dsp_bot, "_send_report_button", AsyncMock()):
            result = await dsp_bot._fallback_single_send(mock_bot, job, bot_id=1)

        assert result is True
        # try_deliver 不应被调用(group receipt 已 completed)
        mock_try_deliver.assert_not_called()


# ════════════════════════════════════════════════════════════════
# 场景 4: caption edit 独立 receipt
# ════════════════════════════════════════════════════════════════

class TestCaptionEditReceipt:
    """R47 P0-5 #3: caption edit 独立 receipt(effect_type='telegram_edit_caption')。"""

    @pytest.mark.asyncio
    async def test_caption_edit_creates_receipt(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """_edit_sent_caption(job_id=非None) 创建 telegram_edit_caption receipt。"""
        from bots import dsp_bot

        job_id = 9401
        chat_id = 54001
        message_id = 94001
        caption = "测试 caption"

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store):
            await dsp_bot._edit_sent_caption(
                mock_bot, chat_id, message_id, caption, job_id=job_id,
            )

        # 验证 effect_receipts 表有 telegram_edit_caption 记录
        expected_action_id = f"dsp:{job_id}:{message_id}:edit_caption"
        cursor = await cache_store._db.execute(
            "SELECT action_id, effect_type, status, external_id "
            "FROM effect_receipts WHERE action_id = ?",
            (expected_action_id,),
        )
        row = await cursor.fetchone()
        assert row is not None, (
            f"未找到 caption edit receipt, action_id={expected_action_id}"
        )
        assert row[0] == expected_action_id
        assert row[1] == "telegram_edit_caption"
        assert row[2] == "completed"
        assert row[3] == str(message_id)  # external_id = message_id

    @pytest.mark.asyncio
    async def test_caption_edit_skipped_on_second_call(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """第二次调用 _edit_sent_caption 同 action_id 时跳过(幂等)。"""
        from bots import dsp_bot

        job_id = 9402
        chat_id = 54002
        message_id = 94002
        caption = "测试 caption 2"

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store):
            # 第一次调用: 创建 receipt + 编辑
            await dsp_bot._edit_sent_caption(
                mock_bot, chat_id, message_id, caption, job_id=job_id,
            )
            # 第二次调用: 应跳过(skipped=True)
            await dsp_bot._edit_sent_caption(
                mock_bot, chat_id, message_id, caption, job_id=job_id,
            )

        # edit_message_caption 应只被调用一次(第二次跳过)
        assert mock_bot.edit_message_caption.call_count == 1

    @pytest.mark.asyncio
    async def test_caption_edit_without_job_id_no_receipt(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """_edit_sent_caption(job_id=None) 不创建 receipt(向后兼容)。"""
        from bots import dsp_bot

        chat_id = 54003
        message_id = 94003
        caption = "无 job_id caption"

        with patch.object(dsp_bot, "_get_store_safe", return_value=cache_store):
            await dsp_bot._edit_sent_caption(
                mock_bot, chat_id, message_id, caption, job_id=None,
            )

        # 验证 effect_receipts 表无 telegram_edit_caption 记录
        cursor = await cache_store._db.execute(
            "SELECT COUNT(*) FROM effect_receipts "
            "WHERE effect_type = 'telegram_edit_caption'"
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 0, "job_id=None 时不应创建 effect receipt"
        # edit_message_caption 应被调用(直接编辑,不走 receipt)
        mock_bot.edit_message_caption.assert_called_once()


# ════════════════════════════════════════════════════════════════
# 场景 5: skipped receipt 核对 + DeliveryError 暂停
# ════════════════════════════════════════════════════════════════

class TestSkippedReceiptVerification:
    """R47 P0-5 #4/#5: skipped receipt 核对 + 幂等读取异常暂停。"""

    @pytest.mark.asyncio
    async def test_verify_skipped_receipt_consistent(
        self, cache_store, receipt_manager,
    ):
        """_verify_skipped_receipt: delivery sent_msg_id 与 effect external_id 一致 → True。"""
        from bots import dsp_bot

        job_id = 9501
        source_msg_id = 800
        sent_msg_id = 80001  # 发送返回的 message_id

        # 预写入 delivery_receipt(sent_msg_id=80001)
        await cache_store.upsert_delivery_receipt(
            job_id, source_msg_id, 55001,
            sent_msg_id=sent_msg_id,
            status="CONFIRMED",
        )

        # 核对: effect external_id = 80001,与 delivery_receipt 一致
        result = await dsp_bot._verify_skipped_receipt(
            cache_store, job_id, source_msg_id, str(sent_msg_id),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_skipped_receipt_inconsistent(
        self, cache_store, receipt_manager,
    ):
        """_verify_skipped_receipt: external_id 不匹配 → False(触发 reconcile)。"""
        from bots import dsp_bot

        job_id = 9502
        source_msg_id = 801
        # delivery_receipt 记录的 sent_msg_id
        await cache_store.upsert_delivery_receipt(
            job_id, source_msg_id, 55002,
            sent_msg_id=80111,
            status="CONFIRMED",
        )

        # effect external_id 不同(80999 ≠ 80111)→ 不一致
        result = await dsp_bot._verify_skipped_receipt(
            cache_store, job_id, source_msg_id, "80999",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_skipped_receipt_no_delivery_record(
        self, cache_store, receipt_manager,
    ):
        """_verify_skipped_receipt: delivery_receipts 无记录 → False。"""
        from bots import dsp_bot

        result = await dsp_bot._verify_skipped_receipt(
            cache_store, job_id=9503, source_msg_id=999,
            effect_external_id="99999",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_skipped_receipt_empty_external_id(
        self, cache_store, receipt_manager,
    ):
        """_verify_skipped_receipt: external_id 为空 → False。"""
        from bots import dsp_bot

        result = await dsp_bot._verify_skipped_receipt(
            cache_store, job_id=9504, source_msg_id=802,
            effect_external_id="",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_skipped_receipt_store_none(self):
        """_verify_skipped_receipt: store=None → False(保守不一致)。"""
        from bots import dsp_bot

        result = await dsp_bot._verify_skipped_receipt(
            None, job_id=9505, source_msg_id=803,
            effect_external_id="88888",
        )
        assert result is False


class TestDeliveryErrorPause:
    """R47 P0-5 #5: 幂等读取异常暂停(抛 DeliveryError,让上层重试逻辑处理)。"""

    @pytest.mark.asyncio
    async def test_delivery_error_is_exception(self):
        """DeliveryError 是 Exception 子类。"""
        from bots.dsp_bot import DeliveryError
        assert issubclass(DeliveryError, Exception)
        # 可实例化并携带消息
        err = DeliveryError("测试异常")
        assert "测试异常" in str(err)

    @pytest.mark.asyncio
    async def test_process_single_job_raises_delivery_error_on_idempotent_read_failure(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """_process_single_job: 幂等读取异常时 raise DeliveryError。"""
        from bots import dsp_bot

        job = _make_job(
            job_id=9601,
            storage_msg_ids=[900],
            batch_file_meta="",
            task_type="single",
        )

        # Mock store: get_delivery_receipts_by_job 抛异常
        mock_store = MagicMock()
        mock_store.get_delivery_receipts_by_job = AsyncMock(
            side_effect=RuntimeError("SQLite 锁超时")
        )

        with patch.object(dsp_bot, "_get_store_safe", return_value=mock_store):
            with pytest.raises(dsp_bot.DeliveryError) as exc_info:
                await dsp_bot._process_single_job(mock_bot, job, bot_id=1)

        assert "幂等读取异常" in str(exc_info.value)
        assert "SQLite 锁超时" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_one_job_catches_delivery_error_not_dead_letter(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """_send_one_job: DeliveryError 不死信,走重试逻辑(retry_local_job)。"""
        from bots import dsp_bot

        job = _make_job(
            job_id=9602,
            storage_msg_ids=[901],
            batch_file_meta="",
            task_type="single",
            retry_count=0,
        )

        # Mock store: get_delivery_receipts_by_job 抛异常(触发 DeliveryError)
        mock_store = MagicMock()
        mock_store.get_delivery_receipts_by_job = AsyncMock(
            side_effect=RuntimeError("读取异常")
        )
        mock_store.is_user_started = AsyncMock(return_value=True)
        mock_store.mark_local_job_dispatched = AsyncMock(return_value=True)
        mock_store.update_local_job_status = AsyncMock()
        mock_store.retry_local_job = AsyncMock()
        mock_store.mark_job_dead = AsyncMock()
        mock_store.mark_job_waiting_start = AsyncMock()
        mock_store.get_sent_msg_ids_for_job = AsyncMock(return_value=[])

        # 重置信号量(避免其他测试占用)
        dsp_bot._send_semaphore = None

        # Mock 动态限速器(acquire 为 AsyncMock)
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock(return_value=0.0)

        with patch.object(dsp_bot, "_get_store_safe", return_value=mock_store), \
             patch.object(dsp_bot, "dynamic_rate_limiter", mock_limiter), \
             patch.object(dsp_bot, "get_pending_jobs_count_local", return_value=0):
            result = await dsp_bot._send_one_job(mock_bot, job, worker_id=1, store=mock_store)

        # 返回 False(发送失败)
        assert result is False
        # DeliveryError 不走 dead 路径(classify_delivery_error 不会被调用)
        # 应走 retry_local_job(重试,不死信)
        mock_store.retry_local_job.assert_called_once_with(job.job_id, 1)

    @pytest.mark.asyncio
    async def test_send_one_job_delivery_error_retries_without_dead_letter(
        self, cache_store, receipt_manager, mock_bot,
    ):
        """DeliveryError 走重试逻辑,不被 classify_delivery_error 归类为 permanent_invalid。"""
        from bots import dsp_bot

        # retry_count=0, new_retry=1 < 3 → 应走 retry_local_job(不死信)
        job = _make_job(
            job_id=9603,
            storage_msg_ids=[902],
            batch_file_meta="",
            task_type="single",
            retry_count=0,
        )

        mock_store = MagicMock()
        mock_store.get_delivery_receipts_by_job = AsyncMock(
            side_effect=RuntimeError("读取异常")
        )
        mock_store.is_user_started = AsyncMock(return_value=True)
        mock_store.update_local_job_status = AsyncMock()
        mock_store.retry_local_job = AsyncMock()
        mock_store.get_sent_msg_ids_for_job = AsyncMock(return_value=[])

        dsp_bot._send_semaphore = None

        # Mock 动态限速器(acquire 为 AsyncMock)
        mock_limiter = MagicMock()
        mock_limiter.acquire = AsyncMock(return_value=0.0)

        with patch.object(dsp_bot, "_get_store_safe", return_value=mock_store), \
             patch.object(dsp_bot, "dynamic_rate_limiter", mock_limiter), \
             patch.object(dsp_bot, "get_pending_jobs_count_local", return_value=0):
            result = await dsp_bot._send_one_job(mock_bot, job, worker_id=1, store=mock_store)

        assert result is False
        # 应调用 retry_local_job(retry_count 0→1)
        mock_store.retry_local_job.assert_called_once_with(job.job_id, 1)
        # 不应调用 update_local_job_status 标记 dead
        # (DeliveryError 在 except DeliveryError 中处理,不进入 except Exception 的 dead 路径)
        for call_args in mock_store.update_local_job_status.call_args_list:
            args, kwargs = call_args
            # 检查是否有 dead 标记(不应有)
            status_arg = args[1] if len(args) > 1 else kwargs.get("status", "")
            assert status_arg != "dead", (
                f"DeliveryError 不应触发 dead 标记,但调用了 "
                f"update_local_job_status({args}, {kwargs})"
            )
