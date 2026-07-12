"""R35 Batch 6: P0-4 Relay/Replication 接线测试。

被测目标:
- ``database.cache_store.CacheStore`` replication_tasks 全状态机:
  - ``create_replication_task`` 返回 task_id(新增返回值)
  - PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED 完整生命周期
  - ``mark_replication_failed`` 失败重试
- ``database.cache_store.CacheStore`` CAS/fencing:
  - ``cas_transition_cell`` Compare-And-Swap 原子转换
  - ``acquire_cell_lease`` / ``release_cell_lease`` 租约互斥
  - topology_version 递增(fencing token)
- ``database.relay_db.RelayDB`` relay_spool 状态机:
  - RECEIVED → BUFFERED → FORWARDING → ACKED
  - Up Bot 不可用时保留 BUFFERED(不删除文件)

测试策略:
- 使用真实 SQLite 临时文件数据库,隔离于生产 data/。
- 验证 R35 §23-25 要求:
  §23 Topology CAS: cells_local.lease_owner/lease_until/transition_id + topology_version
  §24 ReplicationTask: PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED
  §25 RelayExchange: RECEIVED → BUFFERED → FORWARDING → ACKED
- 使用类级 skipif 而非模块级 skip,使 relay_db 测试在 cache_store 不可用时仍能运行。
  原因: conftest 在 Python 3.9 环境下会为 cache_store 注入 MagicMock(Python 3.10+ 语法),
  但 relay_db 可正常加载,因此 relay_spool 测试应继续执行。

对应 R35 Batch 6 P0-4 要求:
- Relay 使用 relay_spool,Up Bot 不可用时保留缓冲
- copy_messages 接入 replication_tasks 状态机
- 拓扑变更使用 CAS + lease fencing
"""
import inspect
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

# ── 延迟导入被测模块(与 conftest 一致) ──────────────────────────
# 注意: 不使用模块级 pytest.skip,改为类级 skipif,使 relay_db 测试
# 在 cache_store 为 MagicMock(Python 3.9 环境)时仍能独立运行。
from database import cache_store as _cs_module
from database import relay_db as _rdb_module

# 判断被测模块是否为真实类(非 MagicMock)
_CACHE_STORE_AVAILABLE = inspect.isclass(_cs_module.CacheStore)
_RELAY_DB_AVAILABLE = inspect.isclass(_rdb_module.RelayDB)

# 绑定真实类(若可用),否则为 None
CacheStore = _cs_module.CacheStore if _CACHE_STORE_AVAILABLE else None
RelayDB = _rdb_module.RelayDB if _RELAY_DB_AVAILABLE else None


# ── Fixture: CacheStore 临时数据库(仅 cache_store 可用时生效) ────

@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例。"""
    tmpdir = tempfile.mkdtemp(prefix="r35_batch6_cache_")
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


# ── Fixture: RelayDB 临时数据库 ─────────────────────────────────

@pytest_asyncio.fixture
async def rdb():
    """创建一个使用临时文件数据库的 RelayDB 实例。"""
    tmpdir = tempfile.mkdtemp(prefix="r35_batch6_relay_")
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
    return f"CODE-B6-{suffix}-{int(time.time() * 1000) % 1000000}"


# ════════════════════════════════════════════════════════════════
# Part 2: replication_tasks 全状态机测试(R35 §24)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _CACHE_STORE_AVAILABLE,
    reason="database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
)
class TestReplicationTasksBatch6:
    """replication_tasks 全状态机: PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED。

    R35 §24 要求:
    - 复制前创建唯一任务 (file_unique_id, src, dst)
    - Telegram copy 返回 dst_msg_id 后先写任务(COPIED_UNVERIFIED)
    - 然后在同一 DB 事务写 Manifest、COMMITTED
    - create_replication_task 返回 task_id(新增返回值)
    """

    @pytest.mark.asyncio
    async def test_create_returns_task_id(self, store):
        """create_replication_task 返回非零 task_id(R35 Batch 6 新增返回值)。"""
        tid = await store.create_replication_task(
            group_id=6001, file_unique_id="b6-fuid-001",
            src_channel_id=7001, dst_channel_id=8001, src_msg_id=9001,
        )
        assert tid > 0, f"create_replication_task 应返回非零 task_id,实际: {tid}"

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_same_task_id(self, store):
        """重复创建(同 UNIQUE)返回已有 task_id(幂等)。"""
        tid1 = await store.create_replication_task(
            group_id=6002, file_unique_id="b6-dup-001",
            src_channel_id=7002, dst_channel_id=8002, src_msg_id=9002,
        )
        tid2 = await store.create_replication_task(
            group_id=6002, file_unique_id="b6-dup-001",
            src_channel_id=7002, dst_channel_id=8002, src_msg_id=99999,
        )
        assert tid1 > 0
        assert tid1 == tid2, "重复创建应返回相同 task_id(幂等)"

    @pytest.mark.asyncio
    async def test_full_lifecycle_planned_to_committed(self, store):
        """完整生命周期: PLANNED → COPYING → COPIED_UNVERIFIED → COMMITTED。"""
        tid = await store.create_replication_task(
            group_id=6003, file_unique_id="b6-life-001",
            src_channel_id=7003, dst_channel_id=8003, src_msg_id=9003,
        )
        assert tid > 0

        # PLANNED → COPYING
        ok = await store.mark_replication_copying(tid)
        assert ok is True, "PLANNED → COPYING 应成功"

        # COPYING → COPIED_UNVERIFIED(写入 dst_msg_id)
        ok = await store.mark_replication_copied(tid, dst_msg_id=9501)
        assert ok is True, "COPYING → COPIED_UNVERIFIED 应成功"

        # COPIED_UNVERIFIED → COMMITTED
        ok = await store.mark_replication_committed(tid)
        assert ok is True, "COPIED_UNVERIFIED → COMMITTED 应成功"

    @pytest.mark.asyncio
    async def test_mark_failed_sets_retry(self, store):
        """复制失败: attempts+1,达到上限置 FAILED,否则保留 PLANNED 重试。"""
        tid = await store.create_replication_task(
            group_id=6004, file_unique_id="b6-fail-001",
            src_channel_id=7004, dst_channel_id=8004, src_msg_id=9004,
        )
        # 第一次失败(attempts=1,未达上限 3)
        ok = await store.mark_replication_failed(tid, "network error", max_attempts=3)
        assert ok is True
        # 任务应仍可重试(status 回到 PLANNED)
        pending = await store.get_pending_replication_tasks(limit=50, priority_max=10)
        matched = [p for p in pending if p["task_id"] == tid]
        assert len(matched) == 1, "未达上限应回到 PLANNED 等待重试"
        assert matched[0]["attempts"] == 1

    @pytest.mark.asyncio
    async def test_mark_failed_max_attempts(self, store):
        """达到 max_attempts 后置 FAILED。"""
        tid = await store.create_replication_task(
            group_id=6005, file_unique_id="b6-fail-002",
            src_channel_id=7005, dst_channel_id=8005, src_msg_id=9005,
        )
        for _ in range(3):
            await store.mark_replication_failed(tid, "persistent error", max_attempts=3)
        # 达到上限后不应出现在 pending 列表
        pending = await store.get_pending_replication_tasks(limit=50, priority_max=10)
        matched = [p for p in pending if p["task_id"] == tid]
        assert len(matched) == 0, "达到 max_attempts 应置 FAILED,不在 pending 中"

    @pytest.mark.asyncio
    async def test_create_with_empty_fuid_returns_zero(self, store):
        """file_unique_id 为空时返回 0(不创建任务)。"""
        tid = await store.create_replication_task(
            group_id=6006, file_unique_id="",
            src_channel_id=7006, dst_channel_id=8006, src_msg_id=9006,
        )
        assert tid == 0


# ════════════════════════════════════════════════════════════════
# Part 3: CAS/fencing 测试(R35 §23)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _CACHE_STORE_AVAILABLE,
    reason="database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
)
class TestCASFencingBatch6:
    """cells_local CAS/fencing: cas_transition_cell + acquire/release_cell_lease。

    R35 §23 要求:
    - Lease/Fencing: cells_local 的 lease_owner/lease_until/transition_id
    - CAS: UPDATE cells_local SET ... WHERE slot_id=? AND status=expected_status
    - topology_version 递增作为 fencing token
    """

    @pytest.mark.asyncio
    async def test_acquire_and_release_lease(self, store):
        """获取租约后能释放,释放后其他 owner 能获取。"""
        slot_id = "a7001"
        # 先插入一条 cell 记录(测试需要)
        await store.bulk_upsert_cells_local([{
            "slot_id": slot_id, "channel_id": 10001, "status": "active",
            "account_name": "test", "group_id": 7001,
        }])
        # mon_bot 获取租约
        ok = await store.acquire_cell_lease(slot_id, owner="mon_bot", lease_seconds=60)
        assert ok is True, "首次获取租约应成功"
        # mon_bot 再次获取(同 owner)应成功
        ok = await store.acquire_cell_lease(slot_id, owner="mon_bot", lease_seconds=60)
        assert ok is True, "同 owner 再次获取应成功"
        # 释放
        await store.release_cell_lease(slot_id, owner="mon_bot")
        # 释放后其他 owner 能获取
        ok = await store.acquire_cell_lease(slot_id, owner="dsp_bot", lease_seconds=60)
        assert ok is True, "释放后其他 owner 获取应成功"
        await store.release_cell_lease(slot_id, owner="dsp_bot")

    @pytest.mark.asyncio
    async def test_lease_contention(self, store):
        """租约竞争: mon_bot 持有时 dsp_bot 获取失败。"""
        slot_id = "a7002"
        await store.bulk_upsert_cells_local([{
            "slot_id": slot_id, "channel_id": 10002, "status": "active",
            "account_name": "test", "group_id": 7002,
        }])
        # mon_bot 获取租约(长租约)
        ok = await store.acquire_cell_lease(slot_id, owner="mon_bot", lease_seconds=300)
        assert ok is True
        # dsp_bot 尝试获取应失败(租约被 mon_bot 持有)
        ok = await store.acquire_cell_lease(slot_id, owner="dsp_bot", lease_seconds=60)
        assert ok is False, "其他 owner 在租约有效期内获取应失败"
        # 清理
        await store.release_cell_lease(slot_id, owner="mon_bot")

    @pytest.mark.asyncio
    async def test_cas_transition_success(self, store):
        """CAS 成功: expected_status 匹配时更新成功 + topology_version 递增。"""
        slot_id = "a7003"
        await store.bulk_upsert_cells_local([{
            "slot_id": slot_id, "channel_id": 10003, "status": "active",
            "account_name": "test", "group_id": 7003,
        }])
        # 读取初始 topology_version
        cells = await store.get_all_cells_local()
        cell = [c for c in cells if c["slot_id"] == slot_id][0]
        initial_version = cell.get("topology_version", 0)
        # CAS: active → shadow1
        ok = await store.cas_transition_cell(
            slot_id, expected_status="active", new_status="shadow1",
            lease_owner="mon_bot", transition_id="test-tid-001",
            lease_seconds=60,
        )
        assert ok is True, "CAS(expected=active) 应成功"
        # 验证状态已变更
        cells = await store.get_all_cells_local()
        cell = [c for c in cells if c["slot_id"] == slot_id][0]
        assert cell["status"] == "shadow1"
        # 验证 topology_version 递增
        new_version = cell.get("topology_version", 0)
        assert new_version > initial_version, f"topology_version 应递增: {initial_version} → {new_version}"
        # 验证 fencing 字段已写入
        assert cell.get("lease_owner") == "mon_bot"
        assert cell.get("transition_id") == "test-tid-001"
        # 清理
        await store.release_cell_lease(slot_id, owner="mon_bot")

    @pytest.mark.asyncio
    async def test_cas_transition_fail_status_mismatch(self, store):
        """CAS 失败: expected_status 不匹配时返回 False(状态已被其他控制面改写)。"""
        slot_id = "a7004"
        await store.bulk_upsert_cells_local([{
            "slot_id": slot_id, "channel_id": 10004, "status": "active",
            "account_name": "test", "group_id": 7004,
        }])
        # CAS: 期望 active,但实际是 active → 成功
        ok = await store.cas_transition_cell(
            slot_id, expected_status="active", new_status="shadow1",
            lease_owner="mon_bot", transition_id="test-tid-002",
        )
        assert ok is True
        # 再次 CAS: 期望 active,但已被改为 shadow1 → 失败
        ok = await store.cas_transition_cell(
            slot_id, expected_status="active", new_status="shadow2",
            lease_owner="dsp_bot", transition_id="test-tid-003",
        )
        assert ok is False, "CAS(expected=active 但实际=shadow1) 应失败"
        # 清理
        await store.release_cell_lease(slot_id, owner="mon_bot")

    @pytest.mark.asyncio
    async def test_cas_with_additional_fields(self, store):
        """CAS 支持 **update_fields 附加字段(如 channel_id, file_count)。"""
        slot_id = "a7005"
        await store.bulk_upsert_cells_local([{
            "slot_id": slot_id, "channel_id": 10005, "status": "shadow1",
            "account_name": "test", "group_id": 7005,
        }])
        # CAS: shadow1 → active,同时更新 channel_id 和 file_count
        ok = await store.cas_transition_cell(
            slot_id, expected_status="shadow1", new_status="active",
            lease_owner="mon_bot", transition_id="test-tid-004",
            channel_id=20005, file_count=42,
        )
        assert ok is True
        cells = await store.get_all_cells_local()
        cell = [c for c in cells if c["slot_id"] == slot_id][0]
        assert cell["status"] == "active"
        assert cell["channel_id"] == 20005
        assert cell["file_count"] == 42
        # 清理
        await store.release_cell_lease(slot_id, owner="mon_bot")


# ════════════════════════════════════════════════════════════════
# Part 1: relay_spool 状态机测试(R35 §25)
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not _RELAY_DB_AVAILABLE,
    reason="database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
)
class TestRelaySpoolBatch6:
    """relay_spool 状态机: RECEIVED → BUFFERED → FORWARDING → ACKED。

    R35 §25 要求:
    - 收到第三方 Bot 媒体时立即创建 spool(RECEIVED)
    - Up Bot 不可用时保留(BUFFERED),不得删除
    - ACK 后才删除临时文件
    """

    @pytest.mark.asyncio
    async def test_create_spool_received(self, rdb):
        """创建 spool 后状态为 RECEIVED,buffered_files 记录文件路径。"""
        code = _make_code("recv")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=101, code=code, user_id=2001,
            external_code=code,
            buffered_files=["/tmp/relay_test_file1.jpg"],
            checksum="sha256:abc123",
            ttl_seconds=3600,
        )
        assert spool_id > 0
        spool = await rdb.get_relay_spool(spool_id)
        assert spool is not None
        assert spool["status"] == "RECEIVED"
        assert spool["buffered_files"] == ["/tmp/relay_test_file1.jpg"]
        assert spool["code"] == code

    @pytest.mark.asyncio
    async def test_up_bot_unavailable_keep_buffered(self, rdb):
        """Up Bot 不可用时: spool 状态保持 BUFFERED,不删除文件。

        模拟 R35 §25 场景: Up Bot 不可用时保留缓冲,不得删除。
        """
        code = _make_code("noup")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=102, code=code, user_id=2002,
            buffered_files=["/tmp/relay_keep1.jpg", "/tmp/relay_keep2.jpg"],
            ttl_seconds=3600,
        )
        # 推进到 BUFFERED(模拟文件已下载,等待 Up Bot)
        ok = await rdb.transition_spool_status(spool_id, "BUFFERED")
        assert ok is True
        # 验证状态为 BUFFERED(文件保留)
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "BUFFERED"
        assert len(spool["buffered_files"]) == 2, "BUFFERED 状态应保留所有文件路径"

    @pytest.mark.asyncio
    async def test_forward_then_ack(self, rdb):
        """完整流程: RECEIVED → BUFFERED → FORWARDING → ACKED。

        ACK 后临时文件可安全删除(由调用方执行)。
        """
        code = _make_code("fwd")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=103, code=code, user_id=2003,
            buffered_files=["/tmp/relay_fwd.jpg"],
            ttl_seconds=3600,
        )
        # RECEIVED → BUFFERED
        await rdb.transition_spool_status(spool_id, "BUFFERED")
        # BUFFERED → FORWARDING
        ok = await rdb.transition_spool_status(spool_id, "FORWARDING")
        assert ok is True
        # FORWARDING → ACKED(Up Bot 确认接收)
        await rdb.ack_relay_spool(spool_id)
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["status"] == "ACKED"
        assert spool["acked_at"] is not None, "ACKED 状态应记录 acked_at"

    @pytest.mark.asyncio
    async def test_spool_idempotent_create(self, rdb):
        """同一 code 可创建多条 spool(不同 spool_id),用于多账号场景。"""
        code = _make_code("idem")
        sid1 = await rdb.create_relay_spool(104, code, 2004, ttl_seconds=300)
        sid2 = await rdb.create_relay_spool(105, code, 2004, ttl_seconds=300)
        assert sid1 > 0
        assert sid2 > 0
        assert sid1 != sid2, "不同 account_id 应创建不同 spool"

    @pytest.mark.asyncio
    async def test_get_active_spool_by_code(self, rdb):
        """get_active_spool_by_code 查询活跃任务(未 ACKED/FAILED)。"""
        code = _make_code("active")
        sid1 = await rdb.create_relay_spool(106, code, 2005, ttl_seconds=300)
        sid2 = await rdb.create_relay_spool(107, code, 2005, ttl_seconds=300)
        await rdb.ack_relay_spool(sid1)  # sid1 已 ACKED
        active = await rdb.get_active_spool_by_code(code)
        active_ids = [s["spool_id"] for s in active]
        assert sid2 in active_ids, "未 ACKED 的 spool 应在活跃列表中"
        assert sid1 not in active_ids, "已 ACKED 的 spool 不应在活跃列表中"

    @pytest.mark.asyncio
    async def test_spool_stats(self, rdb):
        """get_spool_stats 返回各状态计数。"""
        code_base = _make_code("stat")
        s1 = await rdb.create_relay_spool(108, code_base + "1", 2006, ttl_seconds=300)
        s2 = await rdb.create_relay_spool(108, code_base + "2", 2006, ttl_seconds=300)
        await rdb.transition_spool_status(s1, "BUFFERED")
        await rdb.ack_relay_spool(s2)
        stats = await rdb.get_spool_stats()
        assert "RECEIVED" in stats
        assert "BUFFERED" in stats
        assert "ACKED" in stats
        assert stats["BUFFERED"] >= 1
        assert stats["ACKED"] >= 1
