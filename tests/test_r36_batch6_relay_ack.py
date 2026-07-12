"""R36 Batch 6: H6 Relay ACK 语义增强测试。

被测目标:
- ``database.relay_db.RelayDB`` relay_spool H6 细粒度状态机:
  - FORWARDED_TO_UP → UP_DURABLE_ACK → INDEXED → ACKED(已清理)
  - upload_id 关联(get_spool_by_upload_id)
  - 超时未确认检测(get_unacked_spools)
  - INDEXED 清理(get_indexed_spools_for_cleanup)
- ``database.relay_db.RelayDB`` 通用状态更新:
  - update_spool_status(非原子,供 Up Bot / Idx Bot 直接写状态)

H6 核心语义:
- Relay "发送给 Up 成功" ≠ Up/Idx 已完成持久化
- 改为三阶段确认: FORWARDED_TO_UP → UP_DURABLE_ACK → INDEXED
- 临时文件仅在 INDEXED 后才删除(绝不提前删除)

测试策略:
- 使用真实 SQLite 临时文件数据库,隔离于生产 data/。
- 验证状态转换、upload_id 关联、超时检测、临时文件保留语义。
- 使用类级 skipif(与 test_r35_batch6 一致),使测试在 relay_db 可用时独立运行。
"""
import inspect
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

# ── 延迟导入被测模块(与 conftest 一致) ──────────────────────────
from database import relay_db as _rdb_module

# 判断被测模块是否为真实类(非 MagicMock)
_RELAY_DB_AVAILABLE = inspect.isclass(_rdb_module.RelayDB)

# 绑定真实类(若可用),否则为 None
RelayDB = _rdb_module.RelayDB if _RELAY_DB_AVAILABLE else None


# ── Fixture: RelayDB 临时数据库 ─────────────────────────────────

@pytest_asyncio.fixture
async def rdb():
    """创建一个使用临时文件数据库的 RelayDB 实例。"""
    tmpdir = tempfile.mkdtemp(prefix="r36_batch6_relay_ack_")
    db_path = Path(tmpdir) / "test_relay.db"
    original_path = _rdb_module.DB_PATH
    _rdb_module.DB_PATH = db_path
    try:
        instance = RelayDB()
        await instance.init()
        yield instance
        await instance.close()
    finally:
        _rdb_module.DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


def _make_code(suffix="001"):
    return f"CODE-H6-{suffix}-{int(time.time() * 1000) % 1000000}"


# ════════════════════════════════════════════════════════════════
# H6: relay_spool 细粒度状态机测试
# FORWARDED_TO_UP → UP_DURABLE_ACK → INDEXED → ACKED
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6RelayAckStateMachine:
    """H6: relay_spool 细粒度 ACK 状态机。

    状态流: RECEIVED → BUFFERED → FORWARDING → FORWARDED_TO_UP
            → UP_DURABLE_ACK → INDEXED → ACKED(已清理临时文件)
    """

    @pytest.mark.asyncio
    async def test_full_h6_lifecycle(self, rdb):
        """完整 H6 生命周期: FORWARDED_TO_UP → UP_DURABLE_ACK → INDEXED → ACKED。"""
        code = _make_code("life")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=201, code=code, user_id=3001,
            buffered_files=["/tmp/h6_file1.jpg", "/tmp/h6_file2.jpg"],
            ttl_seconds=3600,
        )
        assert spool_id > 0

        # RECEIVED → BUFFERED → FORWARDING
        await rdb.transition_spool_status(spool_id, "BUFFERED")
        await rdb.transition_spool_status(spool_id, "FORWARDING")

        # FORWARDING → FORWARDED_TO_UP(发送给 Up 成功,但不是最终 ACK)
        ok = await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "FORWARDED_TO_UP"
        # 临时文件路径仍保留(未删除)
        assert len(spool["buffered_files"]) == 2

        # FORWARDED_TO_UP → UP_DURABLE_ACK(Up 返回 upload_id)
        ok = await rdb.transition_spool_status(
            spool_id, "UP_DURABLE_ACK", upload_id="upload-session-abc123"
        )
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "UP_DURABLE_ACK"
        assert spool["upload_id"] == "upload-session-abc123"
        # 临时文件仍保留
        assert len(spool["buffered_files"]) == 2

        # UP_DURABLE_ACK → INDEXED(Idx Bot 确认已索引)
        ok = await rdb.update_spool_status(spool_id, "INDEXED")
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "INDEXED"
        # INDEXED 时 acked_at 仍为 None(尚未清理临时文件)
        assert spool["acked_at"] is None
        # 临时文件仍保留(INDEXED 后才由恢复循环清理)
        assert len(spool["buffered_files"]) == 2

        # INDEXED → ACKED(恢复循环清理临时文件后标记)
        ok = await rdb.update_spool_status(spool_id, "ACKED", acked_at=time.time())
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "ACKED"
        assert spool["acked_at"] is not None

    @pytest.mark.asyncio
    async def test_temp_files_preserved_before_indexed(self, rdb):
        """H6 核心约束: 临时文件在 INDEXED 前绝不删除。

        验证 FORWARDED_TO_UP 和 UP_DURABLE_ACK 状态下 buffered_files 完整保留。
        """
        code = _make_code("keep")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=202, code=code, user_id=3002,
            buffered_files=["/tmp/h6_keep1.jpg", "/tmp/h6_keep2.jpg", "/tmp/h6_keep3.jpg"],
            ttl_seconds=3600,
        )
        # 推进到 FORWARDED_TO_UP
        await rdb.transition_spool_status(spool_id, "BUFFERED")
        await rdb.transition_spool_status(spool_id, "FORWARDING")
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")

        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "FORWARDED_TO_UP"
        # 3 个临时文件全部保留
        assert len(spool["buffered_files"]) == 3
        assert "/tmp/h6_keep1.jpg" in spool["buffered_files"]
        assert "/tmp/h6_keep3.jpg" in spool["buffered_files"]

        # 推进到 UP_DURABLE_ACK
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-keep-001")
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "UP_DURABLE_ACK"
        # 仍然 3 个文件
        assert len(spool["buffered_files"]) == 3

    @pytest.mark.asyncio
    async def test_forwarded_to_up_not_terminal(self, rdb):
        """FORWARDED_TO_UP 不是终态,spool 仍出现在活跃列表中。"""
        code = _make_code("active")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=203, code=code, user_id=3003,
            buffered_files=["/tmp/h6_active.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")

        active = await rdb.get_active_spool_by_code(code)
        active_ids = [s["spool_id"] for s in active]
        assert spool_id in active_ids, "FORWARDED_TO_UP 应在活跃列表中(非终态)"

    @pytest.mark.asyncio
    async def test_indexed_is_active_until_cleaned(self, rdb):
        """INDEXED 状态在 acked_at 设置前仍算活跃(待清理)。"""
        code = _make_code("idx")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=204, code=code, user_id=3004,
            buffered_files=["/tmp/h6_idx.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-idx-001")
        await rdb.update_spool_status(spool_id, "INDEXED")

        # INDEXED + acked_at IS NULL → 应在活跃列表中(待清理)
        active = await rdb.get_active_spool_by_code(code)
        active_ids = [s["spool_id"] for s in active]
        assert spool_id in active_ids, "INDEXED(未清理)应在活跃列表中"

        # 设置 acked_at 后(清理完成)→ 不在活跃列表中
        await rdb.update_spool_status(spool_id, "ACKED", acked_at=time.time())
        active = await rdb.get_active_spool_by_code(code)
        active_ids = [s["spool_id"] for s in active]
        assert spool_id not in active_ids, "ACKED(已清理)不应在活跃列表中"


# ════════════════════════════════════════════════════════════════
# H6: upload_id 关联测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6UploadIdAssociation:
    """H6: upload_id 关联 — Up Bot 返回 upload_id 后可查询。"""

    @pytest.mark.asyncio
    async def test_get_spool_by_upload_id(self, rdb):
        """通过 upload_id 查询 spool 记录。"""
        code = _make_code("upid")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=205, code=code, user_id=3005,
            buffered_files=["/tmp/h6_upid.jpg"],
            ttl_seconds=3600,
        )
        upload_id = "upload-session-h6-001"
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id=upload_id)

        # 通过 upload_id 查询
        found = await rdb.get_spool_by_upload_id(upload_id)
        assert found is not None
        assert found["spool_id"] == spool_id
        assert found["upload_id"] == upload_id
        assert found["status"] == "UP_DURABLE_ACK"

    @pytest.mark.asyncio
    async def test_get_spool_by_upload_id_not_found(self, rdb):
        """不存在的 upload_id 返回 None。"""
        found = await rdb.get_spool_by_upload_id("nonexistent-upload-id-999")
        assert found is None

    @pytest.mark.asyncio
    async def test_get_spool_by_empty_upload_id(self, rdb):
        """空 upload_id 返回 None(防御性)。"""
        found = await rdb.get_spool_by_upload_id("")
        assert found is None

    @pytest.mark.asyncio
    async def test_upload_id_via_update_spool_status(self, rdb):
        """update_spool_status 可同时写 upload_id 和状态(供 Up Bot 直接调用)。"""
        code = _make_code("upd")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=206, code=code, user_id=3006,
            buffered_files=["/tmp/h6_upd.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")

        # Up Bot 直接调用 update_spool_status 写 upload_id + UP_DURABLE_ACK
        upload_id = "upload-session-h6-002"
        ok = await rdb.update_spool_status(
            spool_id, "UP_DURABLE_ACK", upload_id=upload_id
        )
        assert ok is True

        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "UP_DURABLE_ACK"
        assert spool["upload_id"] == upload_id
        assert spool["prev_status"] == "FORWARDED_TO_UP"


# ════════════════════════════════════════════════════════════════
# H6: 超时未确认检测测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6UnackedDetection:
    """H6: get_unacked_spools — 检测超时未确认的 spool。"""

    @pytest.mark.asyncio
    async def test_timeout_forwarded_to_up_detected(self, rdb):
        """FORWARDED_TO_UP 超时后被检测为未确认。"""
        code = _make_code("timeout1")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=207, code=code, user_id=3007,
            buffered_files=["/tmp/h6_timeout1.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")

        # 用 0 秒超时阈值(立即超时)查询
        unacked = await rdb.get_unacked_spools(timeout_seconds=0, account_id=207)
        matched = [s for s in unacked if s["spool_id"] == spool_id]
        assert len(matched) == 1, "FORWARDED_TO_UP 超时应被检测到"
        assert matched[0]["status"] == "FORWARDED_TO_UP"

    @pytest.mark.asyncio
    async def test_timeout_up_durable_ack_detected(self, rdb):
        """UP_DURABLE_ACK 超时后被检测为未确认。"""
        code = _make_code("timeout2")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=208, code=code, user_id=3008,
            buffered_files=["/tmp/h6_timeout2.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-timeout-001")

        unacked = await rdb.get_unacked_spools(timeout_seconds=0, account_id=208)
        matched = [s for s in unacked if s["spool_id"] == spool_id]
        assert len(matched) == 1, "UP_DURABLE_ACK 超时应被检测到"
        assert matched[0]["status"] == "UP_DURABLE_ACK"
        assert matched[0]["upload_id"] == "uid-timeout-001"

    @pytest.mark.asyncio
    async def test_no_timeout_not_detected(self, rdb):
        """未超时的 spool 不被检测(大超时阈值)。"""
        code = _make_code("notimeout")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=209, code=code, user_id=3009,
            buffered_files=["/tmp/h6_notimeout.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")

        # 用 3600 秒超时阈值(刚创建的 spool 不会超时)
        unacked = await rdb.get_unacked_spools(timeout_seconds=3600, account_id=209)
        matched = [s for s in unacked if s["spool_id"] == spool_id]
        assert len(matched) == 0, "未超时的 spool 不应被检测到"

    @pytest.mark.asyncio
    async def test_indexed_not_in_unacked(self, rdb):
        """INDEXED 状态不在未确认列表中(已进入清理流程)。"""
        code = _make_code("idxnot")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=210, code=code, user_id=3010,
            buffered_files=["/tmp/h6_idxnot.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-idxnot-001")
        await rdb.update_spool_status(spool_id, "INDEXED")

        unacked = await rdb.get_unacked_spools(timeout_seconds=0, account_id=210)
        matched = [s for s in unacked if s["spool_id"] == spool_id]
        assert len(matched) == 0, "INDEXED 不应在未确认列表中"

    @pytest.mark.asyncio
    async def test_unacked_filter_by_account(self, rdb):
        """get_unacked_spools 按 account_id 过滤。"""
        code1 = _make_code("acct1")
        code2 = _make_code("acct2")
        sid1 = await rdb.create_relay_spool(211, code1, 3011, ttl_seconds=3600)
        sid2 = await rdb.create_relay_spool(212, code2, 3012, ttl_seconds=3600)
        await rdb.transition_spool_status(sid1, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(sid2, "FORWARDED_TO_UP")

        # 查询 account 211
        unacked = await rdb.get_unacked_spools(timeout_seconds=0, account_id=211)
        ids = [s["spool_id"] for s in unacked]
        assert sid1 in ids, "account 211 的 spool 应被检测到"
        assert sid2 not in ids, "account 212 的 spool 不应出现在 account 211 的查询中"

    @pytest.mark.asyncio
    async def test_unacked_all_accounts(self, rdb):
        """get_unacked_spools 不传 account_id 时查询所有账号。"""
        code1 = _make_code("all1")
        code2 = _make_code("all2")
        sid1 = await rdb.create_relay_spool(213, code1, 3013, ttl_seconds=3600)
        sid2 = await rdb.create_relay_spool(214, code2, 3014, ttl_seconds=3600)
        await rdb.transition_spool_status(sid1, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(sid2, "UP_DURABLE_ACK", upload_id="uid-all-001")

        unacked = await rdb.get_unacked_spools(timeout_seconds=0)
        ids = [s["spool_id"] for s in unacked]
        assert sid1 in ids, "account 213 的 spool 应被检测到"
        assert sid2 in ids, "account 214 的 spool 应被检测到"


# ════════════════════════════════════════════════════════════════
# H6: INDEXED 清理测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6IndexedCleanup:
    """H6: get_indexed_spools_for_cleanup — 查找待清理的 INDEXED spool。"""

    @pytest.mark.asyncio
    async def test_indexed_spool_returned_for_cleanup(self, rdb):
        """INDEXED + acked_at IS NULL 的 spool 被返回(待清理)。"""
        code = _make_code("cleanup1")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=215, code=code, user_id=3015,
            buffered_files=["/tmp/h6_cleanup1.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-cleanup1")
        await rdb.update_spool_status(spool_id, "INDEXED")

        indexed = await rdb.get_indexed_spools_for_cleanup(215)
        matched = [s for s in indexed if s["spool_id"] == spool_id]
        assert len(matched) == 1, "INDEXED spool 应被返回(待清理)"
        assert matched[0]["status"] == "INDEXED"
        assert matched[0]["acked_at"] is None

    @pytest.mark.asyncio
    async def test_cleaned_indexed_not_returned(self, rdb):
        """已清理(acked_at 已设置)的 INDEXED→ACKED spool 不被返回。"""
        code = _make_code("cleanup2")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=216, code=code, user_id=3016,
            buffered_files=["/tmp/h6_cleanup2.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-cleanup2")
        await rdb.update_spool_status(spool_id, "INDEXED")
        # 模拟恢复循环清理后标记
        await rdb.update_spool_status(spool_id, "ACKED", acked_at=time.time())

        indexed = await rdb.get_indexed_spools_for_cleanup(216)
        matched = [s for s in indexed if s["spool_id"] == spool_id]
        assert len(matched) == 0, "已清理的 spool 不应被返回"

    @pytest.mark.asyncio
    async def test_non_indexed_not_returned(self, rdb):
        """非 INDEXED 状态的 spool 不被返回。"""
        code = _make_code("cleanup3")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=217, code=code, user_id=3017,
            buffered_files=["/tmp/h6_cleanup3.jpg"],
            ttl_seconds=3600,
        )
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")

        indexed = await rdb.get_indexed_spools_for_cleanup(217)
        matched = [s for s in indexed if s["spool_id"] == spool_id]
        assert len(matched) == 0, "FORWARDED_TO_UP 不应被返回(非 INDEXED)"

    @pytest.mark.asyncio
    async def test_indexed_cleanup_filter_by_account(self, rdb):
        """get_indexed_spools_for_cleanup 按 account_id 过滤。"""
        code1 = _make_code("cleanup4a")
        code2 = _make_code("cleanup4b")
        sid1 = await rdb.create_relay_spool(218, code1, 3018, ttl_seconds=3600)
        sid2 = await rdb.create_relay_spool(219, code2, 3019, ttl_seconds=3600)
        for sid in (sid1, sid2):
            await rdb.transition_spool_status(sid, "FORWARDED_TO_UP")
            await rdb.transition_spool_status(sid, "UP_DURABLE_ACK", upload_id=f"uid-{sid}")
            await rdb.update_spool_status(sid, "INDEXED")

        indexed = await rdb.get_indexed_spools_for_cleanup(218)
        ids = [s["spool_id"] for s in indexed]
        assert sid1 in ids, "account 218 的 INDEXED spool 应被返回"
        assert sid2 not in ids, "account 219 的 spool 不应出现在 account 218 的查询中"


# ════════════════════════════════════════════════════════════════
# H6: update_spool_status 通用更新测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6UpdateSpoolStatus:
    """H6: update_spool_status — 通用状态更新(非原子)。"""

    @pytest.mark.asyncio
    async def test_update_status_with_upload_id(self, rdb):
        """update_spool_status 可同时更新状态和 upload_id。"""
        code = _make_code("upd1")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=220, code=code, user_id=3020,
            buffered_files=["/tmp/h6_upd1.jpg"],
            ttl_seconds=3600,
        )
        ok = await rdb.update_spool_status(
            spool_id, "UP_DURABLE_ACK", upload_id="uid-upd1-001"
        )
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "UP_DURABLE_ACK"
        assert spool["upload_id"] == "uid-upd1-001"
        assert spool["prev_status"] == "RECEIVED"

    @pytest.mark.asyncio
    async def test_update_status_with_acked_at(self, rdb):
        """update_spool_status 可设置 acked_at(清理完成标记)。"""
        code = _make_code("upd2")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=221, code=code, user_id=3021,
            buffered_files=["/tmp/h6_upd2.jpg"],
            ttl_seconds=3600,
        )
        now = time.time()
        ok = await rdb.update_spool_status(spool_id, "ACKED", acked_at=now)
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "ACKED"
        assert spool["acked_at"] == now

    @pytest.mark.asyncio
    async def test_update_status_rejects_invalid_field(self, rdb):
        """update_spool_status 拒绝白名单外的字段(防 SQL 注入)。"""
        code = _make_code("upd3")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=222, code=code, user_id=3022,
            buffered_files=["/tmp/h6_upd3.jpg"],
            ttl_seconds=3600,
        )
        # 非法字段应被跳过(不抛错,但字段不写入)
        ok = await rdb.update_spool_status(
            spool_id, "INDEXED", malicious_field="DROP TABLE"
        )
        assert ok is True
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "INDEXED"

    @pytest.mark.asyncio
    async def test_update_status_nonexistent_spool(self, rdb):
        """更新不存在的 spool_id 返回 False。"""
        ok = await rdb.update_spool_status(99999, "INDEXED")
        assert ok is False


# ════════════════════════════════════════════════════════════════
# H6: get_spool_stats 新状态测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6SpoolStats:
    """H6: get_spool_stats 包含新的细粒度状态。"""

    @pytest.mark.asyncio
    async def test_stats_includes_new_statuses(self, rdb):
        """get_spool_stats 返回的字典包含 H6 新状态键。"""
        stats = await rdb.get_spool_stats()
        assert "FORWARDED_TO_UP" in stats
        assert "UP_DURABLE_ACK" in stats
        assert "INDEXED" in stats
        assert "ACKED" in stats  # 兼容旧状态
        assert "FAILED" in stats

    @pytest.mark.asyncio
    async def test_stats_counts_new_statuses(self, rdb):
        """get_spool_stats 正确计数 H6 新状态。"""
        code_base = _make_code("stat")
        # 创建 2 个 FORWARDED_TO_UP + 1 个 UP_DURABLE_ACK + 1 个 INDEXED
        s1 = await rdb.create_relay_spool(223, code_base + "1", 3023, ttl_seconds=3600)
        s2 = await rdb.create_relay_spool(223, code_base + "2", 3023, ttl_seconds=3600)
        s3 = await rdb.create_relay_spool(223, code_base + "3", 3023, ttl_seconds=3600)
        s4 = await rdb.create_relay_spool(223, code_base + "4", 3023, ttl_seconds=3600)

        await rdb.transition_spool_status(s1, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(s2, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(s3, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(s3, "UP_DURABLE_ACK", upload_id="uid-stat-001")
        await rdb.transition_spool_status(s4, "FORWARDED_TO_UP")
        await rdb.transition_spool_status(s4, "UP_DURABLE_ACK", upload_id="uid-stat-002")
        await rdb.update_spool_status(s4, "INDEXED")

        stats = await rdb.get_spool_stats()
        assert stats["FORWARDED_TO_UP"] >= 2, f"FORWARDED_TO_UP 计数应 >= 2,实际: {stats['FORWARDED_TO_UP']}"
        assert stats["UP_DURABLE_ACK"] >= 1, f"UP_DURABLE_ACK 计数应 >= 1,实际: {stats['UP_DURABLE_ACK']}"
        assert stats["INDEXED"] >= 1, f"INDEXED 计数应 >= 1,实际: {stats['INDEXED']}"


# ════════════════════════════════════════════════════════════════
# H6: 临时文件清理模拟测试(模拟 relay_instance 恢复循环行为)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestH6TempFileCleanupSimulation:
    """H6: 模拟 relay_instance 恢复循环的临时文件清理行为。

    验证:
    - INDEXED 前 buffered_files 完整保留
    - INDEXED 后恢复循环删除临时文件并标记 acked_at
    - 清理后 spool 进入 ACKED 终态
    """

    @pytest.mark.asyncio
    async def test_temp_files_deleted_only_after_indexed(self, rdb, tmp_path):
        """模拟完整流程: 创建临时文件 → FORWARDED_TO_UP → UP_DURABLE_ACK → INDEXED → 清理。

        关键验证: 临时文件在 INDEXED 前存在于磁盘,INDEXED 后被删除。
        """
        # 创建真实临时文件
        tmp_file1 = tmp_path / "h6_real_file1.jpg"
        tmp_file2 = tmp_path / "h6_real_file2.jpg"
        tmp_file1.write_text("fake content 1")
        tmp_file2.write_text("fake content 2")
        file_paths = [str(tmp_file1), str(tmp_file2)]

        code = _make_code("realfile")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=224, code=code, user_id=3024,
            buffered_files=file_paths,
            ttl_seconds=3600,
        )

        # 推进到 FORWARDED_TO_UP(文件应在磁盘上)
        await rdb.transition_spool_status(spool_id, "FORWARDED_TO_UP")
        assert tmp_file1.exists(), "FORWARDED_TO_UP 阶段临时文件不应被删除"
        assert tmp_file2.exists(), "FORWARDED_TO_UP 阶段临时文件不应被删除"

        # 推进到 UP_DURABLE_ACK(文件仍应在磁盘上)
        await rdb.transition_spool_status(spool_id, "UP_DURABLE_ACK", upload_id="uid-real-001")
        assert tmp_file1.exists(), "UP_DURABLE_ACK 阶段临时文件不应被删除"
        assert tmp_file2.exists(), "UP_DURABLE_ACK 阶段临时文件不应被删除"

        # 推进到 INDEXED(文件仍应在磁盘上,恢复循环尚未运行)
        await rdb.update_spool_status(spool_id, "INDEXED")
        assert tmp_file1.exists(), "INDEXED 阶段(清理前)临时文件不应被删除"
        assert tmp_file2.exists(), "INDEXED 阶段(清理前)临时文件不应被删除"

        # 模拟恢复循环: 查询 INDEXED 待清理的 spool
        indexed = await rdb.get_indexed_spools_for_cleanup(224)
        assert len(indexed) == 1
        spool = indexed[0]
        assert spool["spool_id"] == spool_id

        # 模拟 _cleanup_tmp_files 删除临时文件
        for f in spool["buffered_files"]:
            try:
                os.remove(f)
            except OSError:
                pass

        # 模拟恢复循环标记清理完成
        await rdb.update_spool_status(spool_id, "ACKED", acked_at=time.time())

        # 验证临时文件已删除
        assert not tmp_file1.exists(), "清理后临时文件应被删除"
        assert not tmp_file2.exists(), "清理后临时文件应被删除"

        # 验证 spool 进入 ACKED 终态
        final = await rdb.get_relay_spool(spool_id)
        assert final["status"] == "ACKED"
        assert final["acked_at"] is not None

        # 验证已清理的 spool 不再出现在待清理列表中
        indexed_after = await rdb.get_indexed_spools_for_cleanup(224)
        assert len(indexed_after) == 0, "已清理的 spool 不应再出现在待清理列表中"

    @pytest.mark.asyncio
    async def test_backward_compat_acked_still_works(self, rdb):
        """H6 兼容性: 旧的 ack_relay_spool(直接设 ACKED)仍正常工作。"""
        code = _make_code("compat")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=225, code=code, user_id=3025,
            buffered_files=["/tmp/h6_compat.jpg"],
            ttl_seconds=3600,
        )
        # 旧流程: 直接 ACKED
        await rdb.ack_relay_spool(spool_id)
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "ACKED"
        assert spool["acked_at"] is not None

        # ACKED 是终态,不在活跃列表中
        active = await rdb.get_active_spool_by_code(code)
        active_ids = [s["spool_id"] for s in active]
        assert spool_id not in active_ids, "ACKED(旧流程)应不在活跃列表中"

    @pytest.mark.asyncio
    async def test_upload_id_column_exists(self, rdb):
        """H6: relay_spool 表存在 upload_id 列(幂等迁移)。"""
        code = _make_code("col")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=226, code=code, user_id=3026,
            ttl_seconds=3600,
        )
        spool = await rdb.get_relay_spool(spool_id)
        # upload_id 字段应存在且默认为空字符串
        assert "upload_id" in spool
        assert spool["upload_id"] == ""
