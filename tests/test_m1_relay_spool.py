"""M1 业务闭环测试 — relay_db.py relay_spool 表 CRUD。

被测模块: ``database.relay_db.RelayDB`` 中 M1 新增的 relay_spool 表
(中继任务池,持久化中继代发任务,支持崩溃恢复)。

9 个 CRUD 方法:
  - create_relay_spool(创建任务,返回 spool_id)
  - get_relay_spool(按主键查询)
  - get_active_spool_by_code(按 code 查询活跃任务,幂等去重)
  - get_pending_spool_by_account(按账号查询未过期任务,崩溃恢复)
  - transition_spool_status(原子状态迁移)
  - ack_relay_spool(标记 ACKED)
  - fail_relay_spool(累计失败次数,超限置 FAILED)
  - cleanup_expired_spool(TTL 过期清理)
  - get_spool_stats(各状态计数)

状态机: RECEIVED → BUFFERED → FORWARDING → ACKED / FAILED

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 relay_pool.db),
  通过替换 ``database.relay_db.DB_PATH`` 指向临时路径。
- 验证状态机、TTL 过期清理、失败重试上限、计数统计。
- 注意:relay_db.init() 会尝试从 CRDB 恢复账号(异步,失败时回退空池模式),
  测试中不依赖该路径,init() 在 CRDB 不可达时会安全降级。
"""
import inspect
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
# conftest.py 在被测模块不可导入时会注入 MagicMock 占位,
# 此时 RelayDB 不是真实类(inspect.isclass → False),整文件 skip。
from database import relay_db as _rdb_module

if not inspect.isclass(_rdb_module.RelayDB):
    pytest.skip(
        "database.relay_db.RelayDB 不可用(需要 aiosqlite + cryptography)",
        allow_module_level=True,
    )

RelayDB = _rdb_module.RelayDB


# ── Fixture ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def rdb():
    """创建一个使用临时文件数据库的 RelayDB 实例。

    隔离策略:
    1. 临时目录下的 test_relay.db(避免污染项目 data/relay_pool.db)。
    2. 替换 ``database.relay_db.DB_PATH`` 模块属性为 Path 对象。
    3. 结束后 close + shutil.rmtree。

    注意:relay_db.init() 会尝试从 CRDB 恢复账号(失败时安全降级),
    本测试不依赖该路径,CRDB 恢复失败不影响 relay_spool CRUD。
    """
    tmpdir = tempfile.mkdtemp(prefix="m1_relay_test_")
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


# ── 辅助函数 ─────────────────────────────────────────────────────

def _make_code(suffix="001"):
    return f"CODE-{suffix}-{int(time.time() * 1000) % 1000000}"


# ════════════════════════════════════════════════════════════════
# relay_spool 表 CRUD 与状态机测试
# ════════════════════════════════════════════════════════════════

class TestRelaySpool:
    """relay_spool 表 9 个 CRUD 方法测试。

    状态机: RECEIVED → BUFFERED → FORWARDING → ACKED / FAILED
    """

    @pytest.mark.asyncio
    async def test_create_and_get_spool(self, rdb):
        """创建任务后按主键查询,返回 dict 且 status='RECEIVED'。"""
        code = _make_code("create")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=101, code=code, user_id=2001,
            external_code="EXT-001",
            source_msg_ids=[10, 20, 30],
            buffered_files=["/tmp/f1.jpg", "/tmp/f2.jpg"],
            checksum="sha256-abc",
            ttl_seconds=300,
        )
        assert spool_id > 0

        spool = await rdb.get_relay_spool(spool_id)
        assert spool is not None
        assert spool["spool_id"] == spool_id
        assert spool["relay_account_id"] == 101
        assert spool["code"] == code
        assert spool["user_id"] == 2001
        assert spool["external_code"] == "EXT-001"
        assert spool["source_msg_ids"] == [10, 20, 30]
        assert spool["buffered_files"] == ["/tmp/f1.jpg", "/tmp/f2.jpg"]
        assert spool["checksum"] == "sha256-abc"
        assert spool["status"] == "RECEIVED"
        assert spool["prev_status"] is None
        assert spool["attempts"] == 0
        assert spool["ttl_expires_at"] is not None
        assert spool["ttl_expires_at"] > time.time()
        assert spool["acked_at"] is None

    @pytest.mark.asyncio
    async def test_get_relay_spool_not_found(self, rdb):
        """查询不存在的主键返回 None。"""
        result = await rdb.get_relay_spool(999999)
        assert result is None

    @pytest.mark.asyncio
    async def test_create_spool_no_ttl(self, rdb):
        """ttl_seconds <= 0 时 ttl_expires_at 为 None(永不过期)。"""
        code = _make_code("nottl")
        spool_id = await rdb.create_relay_spool(
            relay_account_id=102, code=code, user_id=2002,
            ttl_seconds=0,  # 不设置 TTL
        )
        spool = await rdb.get_relay_spool(spool_id)
        assert spool["ttl_expires_at"] is None

    @pytest.mark.asyncio
    async def test_get_active_by_code(self, rdb):
        """按 code 查询活跃任务(排除 ACKED/FAILED 状态)。"""
        code = _make_code("active")
        # 创建 3 个同 code 的任务
        sid1 = await rdb.create_relay_spool(103, code, 3001, ttl_seconds=300)
        sid2 = await rdb.create_relay_spool(103, code, 3001, ttl_seconds=300)
        sid3 = await rdb.create_relay_spool(103, code, 3001, ttl_seconds=300)

        # 状态1:ACKED(应被排除)
        await rdb.ack_relay_spool(sid1)
        # 状态2:FAILED(应被排除)
        await rdb.fail_relay_spool(sid2, reason="test fail", max_attempts=1)
        # 状态3:仍 RECEIVED(应被返回)

        active = await rdb.get_active_spool_by_code(code)
        active_ids = {s["spool_id"] for s in active}
        assert sid3 in active_ids
        assert sid1 not in active_ids  # ACKED 排除
        assert sid2 not in active_ids  # FAILED 排除

    @pytest.mark.asyncio
    async def test_get_active_by_code_empty(self, rdb):
        """查询不存在的 code 返回空列表。"""
        result = await rdb.get_active_spool_by_code("NONEXISTENT-CODE-XYZ")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_pending_by_account(self, rdb):
        """按账号查询未过期的 RECEIVED 任务(用于崩溃恢复)。"""
        # 创建 3 个任务:2 个未过期 + 1 个已过期
        sid1 = await rdb.create_relay_spool(201, _make_code("p1"), 4001, ttl_seconds=300)
        sid2 = await rdb.create_relay_spool(201, _make_code("p2"), 4001, ttl_seconds=300)
        # 第 3 个 TTL 很短,创建后立刻过期
        sid3 = await rdb.create_relay_spool(201, _make_code("p3"), 4001, ttl_seconds=1)
        # 等待 TTL 过期
        time.sleep(1.2)

        pending = await rdb.get_pending_spool_by_account(201, limit=10)
        pending_ids = {p["spool_id"] for p in pending}
        assert sid1 in pending_ids
        assert sid2 in pending_ids
        assert sid3 not in pending_ids  # TTL 已过期

    @pytest.mark.asyncio
    async def test_get_pending_by_account_no_ttl(self, rdb):
        """ttl_expires_at 为 NULL 的任务被视为永不过期,应被返回。"""
        sid = await rdb.create_relay_spool(
            202, _make_code("nottl"), 4002, ttl_seconds=0,
        )

        pending = await rdb.get_pending_spool_by_account(202, limit=10)
        pending_ids = {p["spool_id"] for p in pending}
        assert sid in pending_ids

    @pytest.mark.asyncio
    async def test_get_pending_by_account_excludes_other_status(self, rdb):
        """已迁移到 BUFFERED/FORWARDING/ACKED/FAILED 的任务不应出现在
        get_pending_spool_by_account(仅查 status='RECEIVED')。"""
        sid1 = await rdb.create_relay_spool(203, _make_code("s1"), 4003, ttl_seconds=300)
        sid2 = await rdb.create_relay_spool(203, _make_code("s2"), 4003, ttl_seconds=300)

        # 将 sid1 迁移到 BUFFERED
        await rdb.transition_spool_status(sid1, "BUFFERED")

        pending = await rdb.get_pending_spool_by_account(203, limit=10)
        pending_ids = {p["spool_id"] for p in pending}
        assert sid2 in pending_ids  # 仍 RECEIVED
        assert sid1 not in pending_ids  # 已 BUFFERED

    @pytest.mark.asyncio
    async def test_transition_status(self, rdb):
        """状态迁移成功:status/prev_status 被更新,返回 True。"""
        sid = await rdb.create_relay_spool(
            301, _make_code("trans"), 5001, ttl_seconds=300,
        )

        ok = await rdb.transition_spool_status(sid, "BUFFERED", reason="files buffered")
        assert ok is True

        spool = await rdb.get_relay_spool(sid)
        assert spool["status"] == "BUFFERED"
        assert spool["prev_status"] == "RECEIVED"
        assert spool["last_error"] == "files buffered"

    @pytest.mark.asyncio
    async def test_transition_same_status_noop(self, rdb):
        """迁移到与当前相同的状态:rowcount=0,返回 False。"""
        sid = await rdb.create_relay_spool(
            302, _make_code("noop"), 5002, ttl_seconds=300,
        )

        # 当前 RECEIVED,迁移到 RECEIVED 应 noop
        ok = await rdb.transition_spool_status(sid, "RECEIVED")
        assert ok is False

        spool = await rdb.get_relay_spool(sid)
        assert spool["status"] == "RECEIVED"  # 不变

    @pytest.mark.asyncio
    async def test_transition_with_update_fields(self, rdb):
        """迁移时通过 **update_fields 更新额外字段(buffered_files/checksum)。"""
        sid = await rdb.create_relay_spool(
            303, _make_code("fields"), 5003, ttl_seconds=300,
        )

        # 迁移到 BUFFERED,同时更新 buffered_files 和 checksum
        ok = await rdb.transition_spool_status(
            sid, "BUFFERED",
            buffered_files='["/tmp/a.jpg", "/tmp/b.jpg"]',
            checksum="sha256-new",
        )
        assert ok is True

        spool = await rdb.get_relay_spool(sid)
        assert spool["status"] == "BUFFERED"
        assert spool["checksum"] == "sha256-new"
        assert spool["buffered_files"] == ["/tmp/a.jpg", "/tmp/b.jpg"]

    @pytest.mark.asyncio
    async def test_ack_spool(self, rdb):
        """ack_relay_spool:状态迁移到 ACKED,acked_at 被设置。"""
        sid = await rdb.create_relay_spool(
            401, _make_code("ack"), 6001, ttl_seconds=300,
        )

        await rdb.ack_relay_spool(sid)

        spool = await rdb.get_relay_spool(sid)
        assert spool["status"] == "ACKED"
        assert spool["prev_status"] == "RECEIVED"
        assert spool["acked_at"] is not None
        assert spool["acked_at"] > 0

    @pytest.mark.asyncio
    async def test_fail_spool_under_max(self, rdb):
        """失败次数未达上限:attempts+1,状态不变(仍可重试)。"""
        sid = await rdb.create_relay_spool(
            501, _make_code("fail1"), 7001, ttl_seconds=300,
        )

        # 第一次失败(max_attempts=3)
        await rdb.fail_relay_spool(sid, reason="network error", max_attempts=3)

        spool = await rdb.get_relay_spool(sid)
        assert spool["attempts"] == 1
        assert spool["status"] == "RECEIVED"  # 状态不变
        assert spool["last_error"] == "network error"

        # 第二次失败
        await rdb.fail_relay_spool(sid, reason="flood wait", max_attempts=3)
        spool = await rdb.get_relay_spool(sid)
        assert spool["attempts"] == 2
        assert spool["status"] == "RECEIVED"  # 仍未达上限

    @pytest.mark.asyncio
    async def test_fail_spool_exceeds_max(self, rdb):
        """失败次数达到上限:status='FAILED'。"""
        sid = await rdb.create_relay_spool(
            502, _make_code("fail2"), 7002, ttl_seconds=300,
        )

        # 连续失败 3 次(max_attempts=3),第三次应触发 FAILED
        for i in range(3):
            await rdb.fail_relay_spool(sid, reason=f"fail-{i}", max_attempts=3)

        spool = await rdb.get_relay_spool(sid)
        assert spool["attempts"] == 3
        assert spool["status"] == "FAILED"
        assert spool["last_error"] == "fail-2"

    @pytest.mark.asyncio
    async def test_fail_spool_default_max_attempts(self, rdb):
        """fail_relay_spool 默认 max_attempts=3。"""
        sid = await rdb.create_relay_spool(
            503, _make_code("fail3"), 7003, ttl_seconds=300,
        )

        # 不传 max_attempts,默认 3
        for i in range(2):
            await rdb.fail_relay_spool(sid, reason=f"fail-{i}")
        spool = await rdb.get_relay_spool(sid)
        assert spool["attempts"] == 2
        assert spool["status"] == "RECEIVED"  # 未达上限

        # 第三次触发 FAILED
        await rdb.fail_relay_spool(sid, reason="fail-2")
        spool = await rdb.get_relay_spool(sid)
        assert spool["status"] == "FAILED"

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, rdb):
        """清理 TTL 过期的 RECEIVED/BUFFERED 任务,置为 FAILED。"""
        # 创建一个已过期的任务(TTL=1秒)
        sid_expired = await rdb.create_relay_spool(
            601, _make_code("exp1"), 8001, ttl_seconds=1,
        )
        # 创建一个未过期的任务
        sid_active = await rdb.create_relay_spool(
            601, _make_code("act1"), 8001, ttl_seconds=300,
        )
        # 创建一个无 TTL 的任务(永不过期)
        sid_no_ttl = await rdb.create_relay_spool(
            601, _make_code("notml"), 8001, ttl_seconds=0,
        )

        # 等待第一个任务过期
        time.sleep(1.2)

        # ttl_seconds=0:清理 ttl_expires_at < now - 0 的任务
        # 即 ttl_expires_at < now 的任务(已过期)
        cleaned = await rdb.cleanup_expired_spool(ttl_seconds=0)
        assert cleaned >= 1

        # 验证已过期的任务被置为 FAILED
        spool_expired = await rdb.get_relay_spool(sid_expired)
        assert spool_expired["status"] == "FAILED"
        assert spool_expired["last_error"] == "TTL expired"

        # 验证未过期的任务未被清理
        spool_active = await rdb.get_relay_spool(sid_active)
        assert spool_active["status"] == "RECEIVED"

        # 无 TTL 的任务也未被清理
        spool_no_ttl = await rdb.get_relay_spool(sid_no_ttl)
        assert spool_no_ttl["status"] == "RECEIVED"

    @pytest.mark.asyncio
    async def test_cleanup_expired_skips_acked(self, rdb):
        """清理不触及 ACKED/FAILED 状态的任务(仅清理 RECEIVED/BUFFERED)。"""
        sid = await rdb.create_relay_spool(
            602, _make_code("acked"), 8002, ttl_seconds=1,
        )
        # 先 ACK,再等待过期
        await rdb.ack_relay_spool(sid)
        time.sleep(1.2)

        cleaned = await rdb.cleanup_expired_spool(ttl_seconds=0)
        # ACKED 不被清理
        assert cleaned == 0

        spool = await rdb.get_relay_spool(sid)
        assert spool["status"] == "ACKED"  # 仍为 ACKED

    @pytest.mark.asyncio
    async def test_get_stats(self, rdb):
        """各状态计数:返回 {RECEIVED, BUFFERED, FORWARDING, ACKED, FAILED}。"""
        # 创建 5 个任务:2 RECEIVED + 1 BUFFERED + 1 ACKED + 1 FAILED
        s1 = await rdb.create_relay_spool(701, _make_code("st1"), 9001, ttl_seconds=300)
        s2 = await rdb.create_relay_spool(701, _make_code("st2"), 9001, ttl_seconds=300)
        s3 = await rdb.create_relay_spool(701, _make_code("st3"), 9001, ttl_seconds=300)
        s4 = await rdb.create_relay_spool(701, _make_code("st4"), 9001, ttl_seconds=300)
        s5 = await rdb.create_relay_spool(701, _make_code("st5"), 9001, ttl_seconds=300)

        await rdb.transition_spool_status(s3, "BUFFERED")
        await rdb.ack_relay_spool(s4)
        await rdb.fail_relay_spool(s5, reason="err", max_attempts=1)

        stats = await rdb.get_spool_stats()
        assert stats["RECEIVED"] == 2
        assert stats["BUFFERED"] == 1
        assert stats["FORWARDING"] == 0
        assert stats["ACKED"] == 1
        assert stats["FAILED"] == 1

    @pytest.mark.asyncio
    async def test_get_stats_empty(self, rdb):
        """空表时所有状态计数为 0。

        R36 H6 兼容: get_spool_stats 已扩展返回新增状态键
        (FORWARDED_TO_UP / UP_DURABLE_ACK / INDEXED),改为按已知键断言。
        """
        stats = await rdb.get_spool_stats()
        assert stats["RECEIVED"] == 0
        assert stats["BUFFERED"] == 0
        assert stats["FORWARDING"] == 0
        assert stats["ACKED"] == 0
        assert stats["FAILED"] == 0

    @pytest.mark.asyncio
    async def test_full_state_machine_flow(self, rdb):
        """完整状态机流程:RECEIVED → BUFFERED → FORWARDING → ACKED。"""
        sid = await rdb.create_relay_spool(
            801, _make_code("flow"), 10001, ttl_seconds=600,
        )

        # RECEIVED → BUFFERED
        ok1 = await rdb.transition_spool_status(sid, "BUFFERED")
        assert ok1 is True
        s1 = await rdb.get_relay_spool(sid)
        assert s1["status"] == "BUFFERED"
        assert s1["prev_status"] == "RECEIVED"

        # BUFFERED → FORWARDING
        ok2 = await rdb.transition_spool_status(sid, "FORWARDING")
        assert ok2 is True
        s2 = await rdb.get_relay_spool(sid)
        assert s2["status"] == "FORWARDING"
        assert s2["prev_status"] == "BUFFERED"

        # FORWARDING → ACKED
        await rdb.ack_relay_spool(sid)
        s3 = await rdb.get_relay_spool(sid)
        assert s3["status"] == "ACKED"
        assert s3["prev_status"] == "FORWARDING"
        assert s3["acked_at"] is not None

        # 验证 ACKED 不再出现在活跃任务中
        active = await rdb.get_active_spool_by_code(s3["code"])
        assert all(s["spool_id"] != sid for s in active)
