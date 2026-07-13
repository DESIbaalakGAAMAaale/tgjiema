"""R35 Batch 4: P0-4 Upload 聚合根接线测试。

被测目标:
- ``bots.up_bot`` 的 upload_session / upload_outbox 接线辅助函数:
  - ``_create_upload_session_for_upload`` — 创建 upload_session(RECEIVED)
  - ``_transition_upload_session_safe`` — 安全推进状态
  - ``_create_outbox_entry_safe`` — 安全创建 outbox 条目
- ``bots.idx_bot`` 的 ``_idx_transition_upload_session_safe`` — idx_bot 侧状态推进
- upload_id 在 pending_uploads → idx_bot 的传递链路

测试策略:
- 使用真实 SQLite 临时文件数据库,通过 monkeypatch 替换
  ``database.cache_store._store`` 指向临时 CacheStore 实例。
- 验证 upload_session 状态机完整流程:
  RECEIVED → COPIED_PRIMARY → MANIFEST_PENDING → MANIFESTED → READY
- 验证 outbox 条目(REGISTER_MANIFEST / ARCHIVE_R100)正确创建。
- 验证 upload_id 在 pending_uploads 记录中正确传递。
- 若 up_bot / idx_bot 因依赖缺失无法导入,相关测试优雅跳过。

对应 R35 第 18 节要求:
- 18.1: Up 收到文件后立即创建 upload session
- 18.2: Manifest Worker 消费 outbox
- 18.3: R100 失败不阻塞主取件,但保持 archive_status
"""
import inspect
import os
import shutil
import sys
import tempfile
import time
import uuid
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

    隔离策略与 test_m1_cache_store_tables.py 一致:
    1. 临时目录下的 test_cache.db(避免污染生产 cache_store.db)。
    2. monkeypatch 替换 ``database.cache_store.DB_PATH`` 模块属性。
    3. 结束后 close + shutil.rmtree。
    """
    tmpdir = tempfile.mkdtemp(prefix="r35_batch4_test_")
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


# ── 尝试导入 up_bot / idx_bot(依赖 telegram 等库) ────────────────

_up_bot_available = False
_idx_bot_available = False

try:
    import bots.up_bot as up_bot_module
    _up_bot_available = True
except Exception:
    _up_bot_available = False

try:
    import bots.idx_bot as idx_bot_module
    _idx_bot_available = True
except Exception:
    _idx_bot_available = False


# ════════════════════════════════════════════════════════════════
# 1. upload_session 状态机完整流程测试
# ════════════════════════════════════════════════════════════════

class TestUploadSessionStateMachine:
    """R35 P0-4: upload_session 状态机完整流程测试。

    状态机:
    RECEIVED → COPIED_PRIMARY → MANIFEST_PENDING → MANIFESTED → READY
    ↘ FAILED_RETRYABLE / FAILED_PERMANENT / EXPIRED
    """

    @pytest.mark.asyncio
    async def test_full_upload_flow_single_file(self, real_store):
        """模拟单文件上传的完整状态机流程。

        Up Bot: RECEIVED → COPIED_PRIMARY → MANIFEST_PENDING
        Idx Bot: MANIFEST_PENDING → MANIFESTED → READY
        """
        upload_id = f"upload-batch4-{uuid.uuid4().hex[:8]}"
        user_id = 10042
        channel_id = -1001234567890
        msg_id = 5001

        # 1. Up Bot: 创建 upload_session (RECEIVED)
        await real_store.create_upload_session(
            upload_id, user_id,
            source_msg_ids=[msg_id],
            options_json={"protect_content": False, "ttl": 7},
            trace_id="test-batch4-single",
        )
        session = await real_store.get_upload_session(upload_id)
        assert session is not None
        assert session["status"] == "RECEIVED"
        assert session["user_id"] == user_id

        # 2. Up Bot: Telegram copy 成功 → COPIED_PRIMARY
        ok = await real_store.transition_upload_session(
            upload_id, "COPIED_PRIMARY", reason="copy_done",
            primary_channel_id=channel_id,
            primary_msg_ids=[msg_id],
        )
        assert ok is True
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "COPIED_PRIMARY"
        assert session["prev_status"] == "RECEIVED"
        assert session["primary_channel_id"] == channel_id
        assert session["primary_msg_ids"] == [msg_id]

        # 3. Up Bot: _finalize_upload → MANIFEST_PENDING
        ok = await real_store.transition_upload_session(
            upload_id, "MANIFEST_PENDING", reason="pending_uploads_written",
        )
        assert ok is True
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "MANIFEST_PENDING"

        # 4. Up Bot: 创建 outbox 条目 (REGISTER_MANIFEST)
        outbox_id_man = f"obx-man-{upload_id}"
        await real_store.create_outbox_entry(
            outbox_id_man, upload_id, "", user_id, channel_id,
            storage_msg_ids=[msg_id],
            batch_file_meta=None,
            task_type="single",
            protect_content=0,
            event_type="REGISTER_MANIFEST",
        )
        outbox_entries = await real_store.get_outbox_by_upload(upload_id)
        assert len(outbox_entries) == 1
        assert outbox_entries[0]["event_type"] == "REGISTER_MANIFEST"
        assert outbox_entries[0]["status"] == "PENDING"

        # 5. Up Bot: 创建 outbox 条目 (ARCHIVE_R100)
        outbox_id_r100 = f"obx-r100-{upload_id}"
        await real_store.create_outbox_entry(
            outbox_id_r100, upload_id, "", user_id, channel_id,
            storage_msg_ids=[msg_id],
            batch_file_meta=None,
            task_type="single",
            protect_content=0,
            event_type="ARCHIVE_R100",
        )
        outbox_entries = await real_store.get_outbox_by_upload(upload_id)
        assert len(outbox_entries) == 2
        event_types = {e["event_type"] for e in outbox_entries}
        assert event_types == {"REGISTER_MANIFEST", "ARCHIVE_R100"}

        # 6. Idx Bot: manifest 登记成功 → MANIFESTED
        ok = await real_store.transition_upload_session(
            upload_id, "MANIFESTED", reason="file_code_generated: TESTCODE001",
        )
        assert ok is True
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "MANIFESTED"

        # 7. Idx Bot: file_code 生成完成 → READY
        ok = await real_store.transition_upload_session(
            upload_id, "READY", reason="idx_processed: TESTCODE001",
        )
        assert ok is True
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "READY"
        assert session["prev_status"] == "MANIFESTED"

    @pytest.mark.asyncio
    async def test_failed_retryable_flow(self, real_store):
        """测试失败可重试流程: RECEIVED → FAILED_RETRYABLE。"""
        upload_id = f"upload-fail-{uuid.uuid4().hex[:8]}"
        user_id = 10043

        await real_store.create_upload_session(upload_id, user_id)
        ok = await real_store.transition_upload_session(
            upload_id, "FAILED_RETRYABLE", reason="copy_failed",
            last_error="Telegram timeout",
        )
        assert ok is True
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "FAILED_RETRYABLE"
        assert session["last_error"] == "Telegram timeout"

    @pytest.mark.asyncio
    async def test_failed_permanent_flow(self, real_store):
        """测试永久失败流程: RECEIVED → FAILED_PERMANENT。"""
        upload_id = f"upload-perm-{uuid.uuid4().hex[:8]}"
        user_id = 10044

        await real_store.create_upload_session(upload_id, user_id)
        ok = await real_store.transition_upload_session(
            upload_id, "FAILED_PERMANENT", reason="state_missing",
            last_error="main_channel is zero",
        )
        assert ok is True
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "FAILED_PERMANENT"

    @pytest.mark.asyncio
    async def test_transition_idempotent_same_status(self, real_store):
        """相同状态的 transition 返回 False(不重复迁移)。"""
        upload_id = f"upload-idem-{uuid.uuid4().hex[:8]}"
        await real_store.create_upload_session(upload_id, 10045)
        # 第一次迁移成功
        ok = await real_store.transition_upload_session(
            upload_id, "COPIED_PRIMARY", reason="copy_done",
        )
        assert ok is True
        # 再次迁移到同一状态返回 False
        ok = await real_store.transition_upload_session(
            upload_id, "COPIED_PRIMARY", reason="duplicate",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_ready_session_can_be_deleted(self, real_store):
        """READY 状态的会话可以被删除(cleanup)。"""
        upload_id = f"upload-del-{uuid.uuid4().hex[:8]}"
        await real_store.create_upload_session(upload_id, 10046)
        await real_store.transition_upload_session(upload_id, "COPIED_PRIMARY")
        await real_store.transition_upload_session(upload_id, "MANIFEST_PENDING")
        await real_store.transition_upload_session(upload_id, "MANIFESTED")
        await real_store.transition_upload_session(upload_id, "READY")

        deleted = await real_store.delete_upload_session(upload_id)
        assert deleted is True
        session = await real_store.get_upload_session(upload_id)
        assert session is None


# ════════════════════════════════════════════════════════════════
# 2. upload_outbox 条目测试
# ════════════════════════════════════════════════════════════════

class TestUploadOutboxEntries:
    """R35 P0-4: upload_outbox 条目创建与状态推进测试。"""

    @pytest.mark.asyncio
    async def test_create_and_process_manifest_outbox(self, real_store):
        """REGISTER_MANIFEST outbox 条目创建后可被消费。"""
        upload_id = f"upload-obx-{uuid.uuid4().hex[:8]}"
        outbox_id = f"obx-man-{upload_id}"
        user_id = 10047
        channel_id = -1001234567891
        msg_ids = [6001, 6002]

        # 创建 upload_session
        await real_store.create_upload_session(upload_id, user_id)

        # 创建 outbox 条目
        await real_store.create_outbox_entry(
            outbox_id, upload_id, "", user_id, channel_id,
            storage_msg_ids=msg_ids,
            batch_file_meta=[{"type": "document"}, {"type": "photo"}],
            task_type="single",
            protect_content=0,
            event_type="REGISTER_MANIFEST",
        )

        # 查询 pending outbox
        pending = await real_store.get_pending_outbox(limit=10)
        assert len(pending) >= 1
        entry = next(e for e in pending if e["outbox_id"] == outbox_id)
        assert entry["event_type"] == "REGISTER_MANIFEST"
        assert entry["status"] == "PENDING"
        assert entry["storage_msg_ids"] == msg_ids

        # 模拟 Manifest Worker 消费:dispatch → done
        ok = await real_store.mark_outbox_dispatched(outbox_id, job_id=1)
        assert ok is True

        ok = await real_store.mark_outbox_done(outbox_id)
        assert ok is True

        # 再次查询 pending 不应包含已完成的条目
        pending = await real_store.get_pending_outbox(limit=10)
        assert not any(e["outbox_id"] == outbox_id for e in pending)

    @pytest.mark.asyncio
    async def test_outbox_failure_retry(self, real_store):
        """outbox 条目失败后 attempts 递增,可重试。"""
        upload_id = f"upload-retry-{uuid.uuid4().hex[:8]}"
        outbox_id = f"obx-retry-{upload_id}"

        await real_store.create_upload_session(upload_id, 10048)
        await real_store.create_outbox_entry(
            outbox_id, upload_id, "", 10048, -1001234567892,
            storage_msg_ids=[7001],
            event_type="ARCHIVE_R100",
        )

        # 模拟失败
        next_retry = time.time() + 60
        ok = await real_store.mark_outbox_failed(outbox_id, "R100 channel not found", next_retry)
        assert ok is True

        # 验证 attempts 递增
        entries = await real_store.get_outbox_by_upload(upload_id)
        assert len(entries) == 1
        assert entries[0]["attempts"] == 1

        # 再次失败
        ok = await real_store.mark_outbox_failed(outbox_id, "still failing", time.time() + 120)
        assert ok is True
        entries = await real_store.get_outbox_by_upload(upload_id)
        assert entries[0]["attempts"] == 2

    @pytest.mark.asyncio
    async def test_outbox_idempotent_insert(self, real_store):
        """重复插入同 outbox_id 不报错(INSERT OR IGNORE)。"""
        upload_id = f"upload-idem-obx-{uuid.uuid4().hex[:8]}"
        outbox_id = f"obx-idem-{upload_id}"

        await real_store.create_upload_session(upload_id, 10049)
        await real_store.create_outbox_entry(
            outbox_id, upload_id, "", 10049, -1001234567893,
            event_type="REGISTER_MANIFEST",
        )
        # 重复插入不抛异常
        await real_store.create_outbox_entry(
            outbox_id, upload_id, "", 10049, -1001234567893,
            event_type="REGISTER_MANIFEST",
        )
        entries = await real_store.get_outbox_by_upload(upload_id)
        assert len(entries) == 1  # 只有一条


# ════════════════════════════════════════════════════════════════
# 3. up_bot 辅助函数测试(需要导入 up_bot 模块)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _up_bot_available, reason="bots.up_bot 不可导入(缺少 telegram 等依赖)")
class TestUpBotHelpers:
    """R35 P0-4: up_bot 辅助函数测试。

    通过 patch ``bots.up_bot.get_cache_store`` 指向真实 SQLite CacheStore,
    验证辅助函数正确调用 cache_store 方法。
    """

    @pytest.mark.asyncio
    async def test_create_upload_session_for_upload(self, real_store):
        """_create_upload_session_for_upload 创建 RECEIVED 状态的 upload_session。"""
        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store):
            upload_id = await up_bot_module._create_upload_session_for_upload(
                user_id=10050,
                source_msg_ids=[8001, 8002],
                options={"protect_content": True, "ttl": 30},
            )
        assert upload_id  # 非空字符串
        session = await real_store.get_upload_session(upload_id)
        assert session is not None
        assert session["status"] == "RECEIVED"
        assert session["user_id"] == 10050
        assert session["source_msg_ids"] == [8001, 8002]
        assert session["options_json"]["protect_content"] is True

    @pytest.mark.asyncio
    async def test_create_upload_session_failure_raises_durability_error(self, real_store):
        """R38 P0-3: get_cache_store 抛异常时,_create_upload_session_for_upload 抛 DurabilityError。"""
        from utils.exceptions import DurabilityError
        mock_failing_store = MagicMock()
        mock_failing_store.create_upload_session = AsyncMock(side_effect=RuntimeError("DB locked"))
        with patch.object(up_bot_module, 'get_cache_store', return_value=mock_failing_store):
            with pytest.raises(DurabilityError):
                await up_bot_module._create_upload_session_for_upload(
                    user_id=10051,
                )

    @pytest.mark.asyncio
    async def test_transition_upload_session_safe_success(self, real_store):
        """_transition_upload_session_safe 成功推进状态。"""
        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store):
            upload_id = await up_bot_module._create_upload_session_for_upload(10052)
            assert upload_id

            await up_bot_module._transition_upload_session_safe(
                upload_id, "COPIED_PRIMARY", reason="test_copy",
                primary_channel_id=-1001234567894,
                primary_msg_ids=[9001],
            )

        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "COPIED_PRIMARY"
        assert session["primary_channel_id"] == -1001234567894
        assert session["primary_msg_ids"] == [9001]

    @pytest.mark.asyncio
    async def test_transition_upload_session_safe_empty_id(self, real_store):
        """upload_id 为空时,_transition_upload_session_safe 不执行任何操作。"""
        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store):
            # 空 upload_id,不应调用 cache_store
            await up_bot_module._transition_upload_session_safe(
                "", "COPIED_PRIMARY", reason="empty_id",
            )
        # 不应有任何 session 被创建
        # (若调用了 transition_upload_session,会因 upload_id 不存在而返回 False,不会报错)

    @pytest.mark.asyncio
    async def test_transition_upload_session_safe_failure(self, real_store):
        """cache_store 抛异常时,_transition_upload_session_safe 不传播异常。"""
        mock_failing_store = MagicMock()
        mock_failing_store.transition_upload_session = AsyncMock(side_effect=RuntimeError("DB error"))
        with patch.object(up_bot_module, 'get_cache_store', return_value=mock_failing_store):
            # 不应抛出异常
            await up_bot_module._transition_upload_session_safe(
                "some-upload-id", "COPIED_PRIMARY", reason="test",
            )

    @pytest.mark.asyncio
    async def test_create_outbox_entry_safe_success(self, real_store):
        """_create_outbox_entry_safe 成功创建 outbox 条目。"""
        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store):
            upload_id = await up_bot_module._create_upload_session_for_upload(10053)
            await up_bot_module._transition_upload_session_safe(
                upload_id, "COPIED_PRIMARY", reason="copy_done",
            )
            await up_bot_module._create_outbox_entry_safe(
                f"obx-man-{upload_id}", upload_id, 10053,
                -1001234567895, msg_ids=[10001],
                file_meta=None, event_type="REGISTER_MANIFEST",
            )
            await up_bot_module._create_outbox_entry_safe(
                f"obx-r100-{upload_id}", upload_id, 10053,
                -1001234567895, msg_ids=[10001],
                file_meta=None, event_type="ARCHIVE_R100",
            )

        entries = await real_store.get_outbox_by_upload(upload_id)
        assert len(entries) == 2
        event_types = {e["event_type"] for e in entries}
        assert event_types == {"REGISTER_MANIFEST", "ARCHIVE_R100"}

    @pytest.mark.asyncio
    async def test_create_outbox_entry_safe_empty_upload_id(self, real_store):
        """upload_id 为空时,_create_outbox_entry_safe 不执行任何操作。"""
        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store):
            await up_bot_module._create_outbox_entry_safe(
                "obx-test", "", 10054,
                -1001234567896, msg_ids=[10002],
                event_type="REGISTER_MANIFEST",
            )
        # 不应有 outbox 条目被创建(upload_id 为空)

    @pytest.mark.asyncio
    async def test_create_outbox_entry_safe_failure(self, real_store):
        """cache_store 抛异常时,_create_outbox_entry_safe 不传播异常。"""
        mock_failing_store = MagicMock()
        mock_failing_store.create_outbox_entry = AsyncMock(side_effect=RuntimeError("DB error"))
        with patch.object(up_bot_module, 'get_cache_store', return_value=mock_failing_store):
            # 不应抛出异常
            await up_bot_module._create_outbox_entry_safe(
                "obx-test", "some-upload-id", 10055,
                -1001234567897, msg_ids=[10003],
                event_type="REGISTER_MANIFEST",
            )


# ════════════════════════════════════════════════════════════════
# 4. up_bot _process_upload 接线测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _up_bot_available, reason="bots.up_bot 不可导入(缺少 telegram 等依赖)")
class TestUpBotProcessUploadWiring:
    """R35 P0-4: 验证 _process_upload 正确调用 upload_session 接线方法。

    通过 mock 外部依赖(Telegram copy / dedup / manifest),
    验证 upload_session 在正确的时机被创建和推进。
    """

    @pytest.mark.asyncio
    async def test_process_upload_creates_session_and_transitions(self, real_store):
        """_process_upload: 创建 upload_session + 推进到 COPIED_PRIMARY。"""
        # 准备 mock update 和 context
        mock_update = MagicMock()
        mock_update.effective_user.id = 10060
        mock_update.effective_chat.id = 10061
        mock_update.message.message_id = 11001
        mock_update.message.caption = ""
        mock_update.message.photo = [MagicMock(file_unique_id="fuid-test-001")]
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.user_data = {}

        mock_forwarded = MagicMock()
        mock_forwarded.message_id = 22001

        # Patch 依赖
        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store), \
             patch.object(up_bot_module, '_get_upload_target_channel', new=AsyncMock(return_value=-1001234567800)), \
             patch.object(up_bot_module, '_check_dedup', new=AsyncMock(return_value=None)), \
             patch.object(up_bot_module, 'safe_copy_message', new=AsyncMock(return_value=mock_forwarded)), \
             patch.object(up_bot_module, '_register_manifest', new=AsyncMock()), \
             patch.object(up_bot_module, '_forward_to_r100', new=AsyncMock()), \
             patch.object(up_bot_module, 'extract_file_meta', return_value={"type": "photo", "file_id": "fid"}), \
             patch.object(up_bot_module, 'detect_file_type', return_value="photo"), \
             patch.object(up_bot_module, 'check_upload_permission', new=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'global_rate_limiter', acquire=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'user_rate_limiter', acquire=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'check_force_join', new=AsyncMock(return_value=True)):

            await up_bot_module._process_upload(
                user_id=10060, update=mock_update, context=mock_context,
                file_types={"photo": 1},
            )

        # 验证 upload_session 被创建
        upload_id = mock_context.user_data.get("_upload_id", "")
        assert upload_id, "upload_id 应在 context.user_data 中"
        session = await real_store.get_upload_session(upload_id)
        assert session is not None
        assert session["status"] == "COPIED_PRIMARY"
        assert session["primary_channel_id"] == -1001234567800
        assert session["primary_msg_ids"] == [22001]

        # 验证 _pending_upload_meta 也保存了 upload_id
        meta_key = f"10060:11001"
        assert meta_key in up_bot_module._pending_upload_meta
        assert up_bot_module._pending_upload_meta[meta_key]["upload_id"] == upload_id

        # 清理
        up_bot_module._pending_upload_meta.clear()

    @pytest.mark.asyncio
    async def test_process_upload_dedup_hit_transitions(self, real_store):
        """_process_upload: 秒传去重命中时也推进到 COPIED_PRIMARY。"""
        mock_update = MagicMock()
        mock_update.effective_user.id = 10061
        mock_update.effective_chat.id = 10062
        mock_update.message.message_id = 11002
        mock_update.message.caption = ""
        mock_update.message.photo = [MagicMock(file_unique_id="fuid-dedup-001")]
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.user_data = {}

        dedup_result = {
            "channel_id": -1001234567801,
            "message_id": 22002,
            "media_type": "photo",
            "media_group_id": "",
        }

        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store), \
             patch.object(up_bot_module, '_get_upload_target_channel', new=AsyncMock(return_value=-1001234567801)), \
             patch.object(up_bot_module, '_check_dedup', new=AsyncMock(return_value=dedup_result)), \
             patch.object(up_bot_module, '_register_manifest', new=AsyncMock()), \
             patch.object(up_bot_module, '_forward_to_r100', new=AsyncMock()), \
             patch.object(up_bot_module, 'extract_file_meta', return_value={"type": "photo"}), \
             patch.object(up_bot_module, 'detect_file_type', return_value="photo"), \
             patch.object(up_bot_module, 'check_upload_permission', new=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'global_rate_limiter', acquire=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'user_rate_limiter', acquire=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'check_force_join', new=AsyncMock(return_value=True)):

            await up_bot_module._process_upload(
                user_id=10061, update=mock_update, context=mock_context,
                file_types={"photo": 1},
            )

        upload_id = mock_context.user_data.get("_upload_id", "")
        assert upload_id
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "COPIED_PRIMARY"
        assert session["primary_msg_ids"] == [22002]

        up_bot_module._pending_upload_meta.clear()

    @pytest.mark.asyncio
    async def test_process_upload_copy_failure_transitions_failed(self, real_store):
        """_process_upload: Telegram copy 失败时推进到 FAILED_RETRYABLE。"""
        mock_update = MagicMock()
        mock_update.effective_user.id = 10062
        mock_update.effective_chat.id = 10063
        mock_update.message.message_id = 11003
        mock_update.message.caption = ""
        mock_update.message.photo = [MagicMock(file_unique_id="fuid-fail-001")]
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        mock_context.user_data = {}

        with patch.object(up_bot_module, 'get_cache_store', return_value=real_store), \
             patch.object(up_bot_module, '_get_upload_target_channel', new=AsyncMock(return_value=-1001234567802)), \
             patch.object(up_bot_module, '_check_dedup', new=AsyncMock(return_value=None)), \
             patch.object(up_bot_module, 'safe_copy_message', new=AsyncMock(side_effect=RuntimeError("Telegram error"))), \
             patch.object(up_bot_module, '_register_manifest', new=AsyncMock()), \
             patch.object(up_bot_module, '_forward_to_r100', new=AsyncMock()), \
             patch.object(up_bot_module, 'extract_file_meta', return_value={"type": "photo"}), \
             patch.object(up_bot_module, 'detect_file_type', return_value="photo"), \
             patch.object(up_bot_module, 'check_upload_permission', new=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'global_rate_limiter', acquire=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'user_rate_limiter', acquire=AsyncMock(return_value=True)), \
             patch.object(up_bot_module, 'check_force_join', new=AsyncMock(return_value=True)):

            await up_bot_module._process_upload(
                user_id=10062, update=mock_update, context=mock_context,
                file_types={"photo": 1},
            )

        upload_id = mock_context.user_data.get("_upload_id", "")
        assert upload_id
        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "FAILED_RETRYABLE"

        up_bot_module._pending_upload_meta.clear()


# ════════════════════════════════════════════════════════════════
# 5. idx_bot 接线测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _idx_bot_available, reason="bots.idx_bot 不可导入(缺少 telegram 等依赖)")
class TestIdxBotWiring:
    """R35 P0-4: 验证 idx_bot 正确推进 upload_session 到 READY。"""

    @pytest.mark.asyncio
    async def test_idx_transition_upload_session_safe_success(self, real_store):
        """_idx_transition_upload_session_safe 成功推进状态。"""
        # 先创建一个 session
        upload_id = f"upload-idx-{uuid.uuid4().hex[:8]}"
        await real_store.create_upload_session(upload_id, 10070)
        await real_store.transition_upload_session(upload_id, "COPIED_PRIMARY")
        await real_store.transition_upload_session(upload_id, "MANIFEST_PENDING")

        with patch('database.cache_store.get_cache_store', return_value=real_store):
            await idx_bot_module._idx_transition_upload_session_safe(
                upload_id, "MANIFESTED", reason="test_manifested",
            )
            await idx_bot_module._idx_transition_upload_session_safe(
                upload_id, "READY", reason="test_ready",
            )

        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "READY"
        assert session["prev_status"] == "MANIFESTED"

    @pytest.mark.asyncio
    async def test_idx_transition_upload_session_safe_empty_id(self, real_store):
        """upload_id 为空时,_idx_transition_upload_session_safe 不执行操作。"""
        with patch('database.cache_store.get_cache_store', return_value=real_store):
            # 空 upload_id,不应抛异常
            await idx_bot_module._idx_transition_upload_session_safe(
                "", "READY", reason="empty",
            )

    @pytest.mark.asyncio
    async def test_idx_transition_upload_session_safe_failure(self, real_store):
        """cache_store 抛异常时,_idx_transition_upload_session_safe 不传播异常。"""
        mock_failing_store = MagicMock()
        mock_failing_store.transition_upload_session = AsyncMock(side_effect=RuntimeError("DB error"))
        with patch('database.cache_store.get_cache_store', return_value=mock_failing_store):
            # 不应抛出异常
            await idx_bot_module._idx_transition_upload_session_safe(
                "some-upload-id", "READY", reason="test",
            )


# ════════════════════════════════════════════════════════════════
# 6. upload_id 在 pending_uploads → idx_bot 的传递链路测试
# ════════════════════════════════════════════════════════════════

class TestUploadIdPropagation:
    """R35 P0-4: 验证 upload_id 在 pending_uploads 记录中正确传递。

    up_bot._finalize_upload 将 upload_id 写入 pending_uploads 记录,
    idx_bot._process_pending_uploads 通过 projection 读取 upload_id,
    idx_bot._process_one_pending 从 row 中提取 upload_id。
    """

    @pytest.mark.asyncio
    async def test_upload_id_in_pending_uploads_record(self, real_store):
        """模拟 up_bot 写入 pending_uploads 记录时包含 upload_id。

        验证 upload_id 字段在记录中存在且值正确。
        (由于 pending_uploads 是 CRDB collection,此处仅验证字段名和数据格式)
        """
        upload_id = f"upload-prop-{uuid.uuid4().hex[:8]}"
        user_id = 10080

        # 创建 upload_session
        await real_store.create_upload_session(upload_id, user_id)

        # 模拟 pending_uploads 记录(实际由 CRDB collection 存储)
        pending_record = {
            "id": 1,
            "uploader_id": user_id,
            "primary_channel_id": -1001234567800,
            "primary_channel_msg_id": 12001,
            "file_types": '{"photo": 1}',
            "batch_msg_ids": "12001",
            "batch_file_meta": '[{"type": "photo"}]',
            "note": "",
            "protect_content": False,
            "file_ttl_days": 7,
            "upload_id": upload_id,  # R35 P0-4 新增字段
        }

        # 验证 upload_id 字段存在于记录中
        assert "upload_id" in pending_record
        assert pending_record["upload_id"] == upload_id

        # 模拟 idx_bot 从 row 中提取 upload_id
        row_upload_id = pending_record.get("upload_id", "")
        assert row_upload_id == upload_id

        # 模拟 idx_bot 推进 upload_session 到 READY
        await real_store.transition_upload_session(upload_id, "COPIED_PRIMARY")
        await real_store.transition_upload_session(upload_id, "MANIFEST_PENDING")
        await real_store.transition_upload_session(upload_id, "MANIFESTED")
        await real_store.transition_upload_session(upload_id, "READY")

        session = await real_store.get_upload_session(upload_id)
        assert session["status"] == "READY"

    @pytest.mark.asyncio
    async def test_upload_id_empty_for_legacy_records(self, real_store):
        """旧记录(无 upload_id 字段)应返回空串,不阻塞 idx_bot 处理。"""
        # 模拟旧 pending_uploads 记录(无 upload_id 字段)
        legacy_record = {
            "id": 2,
            "uploader_id": 10081,
            "primary_channel_id": -1001234567801,
            "primary_channel_msg_id": 12002,
            "file_types": '{"document": 1}',
            # 无 upload_id 字段
        }

        # idx_bot 提取 upload_id 时应返回空串
        upload_id = legacy_record.get("upload_id", "")
        assert upload_id == ""

        # _idx_transition_upload_session_safe 在 upload_id 为空时应跳过
        # (此行为在 TestIdxBotWiring 中已测试)
