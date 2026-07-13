"""R40 P1-11: 临时封禁自动解封执行器测试。

被测能力:
- ban_user 持久化 ban_expires_at(临时封禁写到期时间,永久封禁写 NULL)
- unban_user 清除 ban_expires_at
- is_user_banned / check_user_banned 过期自动解封
- cleanup_expired_bans 批量解封过期封禁

测试策略:
- 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db),
  通过 monkeypatch 替换 database.cache_store.DB_PATH 指向临时路径。
- monkeypatch services.content_reports.get_cache_store 返回测试 store,
  让 ban_user 等函数操作测试库。
- 通过直接 UPDATE ban_expires_at 为过去时间模拟封禁到期,验证自动解封。
"""
import inspect
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


@pytest_asyncio.fixture
async def store(monkeypatch):
    """创建使用临时文件数据库的 CacheStore 实例,并注入到 content_reports 模块。"""
    tmpdir = tempfile.mkdtemp(prefix="r40_p1_11_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        # 注入测试 store 到 content_reports 模块
        from services import content_reports
        monkeypatch.setattr(content_reports, "get_cache_store", lambda: s)
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _insert_user(store, user_id: int = 1001):
    """插入一个测试用户到 users_local。"""
    await store.upsert_user_local({
        "user_id": user_id,
        "username": "tester",
        "first_name": "Test",
        "is_banned": 0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }, mark_dirty=False)


async def _get_ban_fields(store, user_id: int) -> dict:
    """直接查询 users_local 的封禁字段。"""
    rows = await store._db.execute_fetchall(
        "SELECT is_banned, ban_expires_at FROM users_local WHERE user_id = ?",
        (user_id,),
    )
    if not rows:
        return {"is_banned": 0, "ban_expires_at": None}
    r = rows[0]
    return {"is_banned": int(r[0] or 0), "ban_expires_at": r[1]}


# ════════════════════════════════════════════════════════════════
# P1-11-1: 临时封禁写入 ban_expires_at
# ════════════════════════════════════════════════════════════════

class TestTempBanPersistExpiry:
    """ban_user 持久化 ban_expires_at 字段。"""

    @pytest.mark.asyncio
    async def test_temp_ban_writes_expires_at(self, store):
        """临时封禁(duration_days>0)写入非空 ban_expires_at。"""
        from services.content_reports import ban_user
        await _insert_user(store, 1001)
        ok = await ban_user(user_id=1001, reason="spam", duration_days=1, admin_id=999)
        assert ok is True
        fields = await _get_ban_fields(store, 1001)
        assert fields["is_banned"] == 1
        assert fields["ban_expires_at"] is not None
        # ban_expires_at 应为可解析的 ISO 时间,且在未来
        expires_dt = datetime.fromisoformat(fields["ban_expires_at"])
        assert expires_dt > datetime.now()

    @pytest.mark.asyncio
    async def test_permanent_ban_writes_null_expires_at(self, store):
        """永久封禁(duration_days=0)写入 ban_expires_at=NULL。"""
        from services.content_reports import ban_user
        await _insert_user(store, 1002)
        ok = await ban_user(user_id=1002, reason="abuse", duration_days=0, admin_id=999)
        assert ok is True
        fields = await _get_ban_fields(store, 1002)
        assert fields["is_banned"] == 1
        assert fields["ban_expires_at"] is None


# ════════════════════════════════════════════════════════════════
# P1-11-2: is_user_banned 过期自动解封
# ════════════════════════════════════════════════════════════════

class TestAutoUnbanOnExpiry:
    """is_user_banned / check_user_banned 过期自动解封。"""

    @pytest.mark.asyncio
    async def test_is_user_banned_auto_unban_expired(self, store):
        """封禁到期后 is_user_banned 返回 False 并自动解封。"""
        from services.content_reports import ban_user, is_user_banned
        await _insert_user(store, 2001)
        await ban_user(user_id=2001, reason="spam", duration_days=1, admin_id=999)
        # 手动将到期时间改为过去,模拟封禁过期
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        await store._db.execute(
            "UPDATE users_local SET ban_expires_at = ? WHERE user_id = ?",
            (past, 2001),
        )
        await store._db.commit()
        banned = await is_user_banned(2001)
        assert banned is False
        # 验证已自动解封(is_banned 被置 0, ban_expires_at 被清空)
        fields = await _get_ban_fields(store, 2001)
        assert fields["is_banned"] == 0
        assert fields["ban_expires_at"] is None

    @pytest.mark.asyncio
    async def test_is_user_banned_not_expired_still_banned(self, store):
        """封禁未到期时 is_user_banned 返回 True。"""
        from services.content_reports import ban_user, is_user_banned
        await _insert_user(store, 2002)
        await ban_user(user_id=2002, reason="spam", duration_days=1, admin_id=999)
        banned = await is_user_banned(2002)
        assert banned is True

    @pytest.mark.asyncio
    async def test_is_user_banned_permanent_stays_banned(self, store):
        """永久封禁(NULL 到期)不会自动解封。"""
        from services.content_reports import ban_user, is_user_banned
        await _insert_user(store, 2003)
        await ban_user(user_id=2003, reason="abuse", duration_days=0, admin_id=999)
        banned = await is_user_banned(2003)
        assert banned is True
        fields = await _get_ban_fields(store, 2003)
        assert fields["is_banned"] == 1
        assert fields["ban_expires_at"] is None

    @pytest.mark.asyncio
    async def test_check_user_banned_alias_auto_unban(self, store):
        """check_user_banned 兼容别名同样支持自动解封。"""
        from services.content_reports import ban_user, check_user_banned
        await _insert_user(store, 2004)
        await ban_user(user_id=2004, reason="spam", duration_days=1, admin_id=999)
        past = (datetime.now() - timedelta(minutes=10)).isoformat()
        await store._db.execute(
            "UPDATE users_local SET ban_expires_at = ? WHERE user_id = ?",
            (past, 2004),
        )
        await store._db.commit()
        banned = await check_user_banned(2004)
        assert banned is False
        fields = await _get_ban_fields(store, 2004)
        assert fields["is_banned"] == 0

    @pytest.mark.asyncio
    async def test_is_user_banned_unbanned_user(self, store):
        """未封禁用户 is_user_banned 返回 False。"""
        from services.content_reports import is_user_banned
        await _insert_user(store, 2005)
        banned = await is_user_banned(2005)
        assert banned is False


# ════════════════════════════════════════════════════════════════
# P1-11-3: cleanup_expired_bans 批量解封
# ════════════════════════════════════════════════════════════════

class TestCleanupExpiredBans:
    """cleanup_expired_bans 批量解封过期封禁。"""

    @pytest.mark.asyncio
    async def test_cleanup_returns_count_and_unbans(self, store):
        """批量清理过期封禁,返回解封数量。"""
        from services.content_reports import ban_user, cleanup_expired_bans
        past = (datetime.now() - timedelta(hours=2)).isoformat()
        # 插入 3 个用户并临时封禁
        for uid in (3001, 3002, 3003):
            await _insert_user(store, uid)
            await ban_user(user_id=uid, reason="spam", duration_days=1, admin_id=999)
            # 全部改为过期
            await store._db.execute(
                "UPDATE users_local SET ban_expires_at = ? WHERE user_id = ?",
                (past, uid),
            )
        # 一个未过期的临时封禁(不应被清理)
        await _insert_user(store, 3004)
        await ban_user(user_id=3004, reason="spam", duration_days=1, admin_id=999)
        # 一个永久封禁(不应被清理)
        await _insert_user(store, 3005)
        await ban_user(user_id=3005, reason="abuse", duration_days=0, admin_id=999)
        await store._db.commit()

        count = await cleanup_expired_bans()
        assert count == 3
        # 验证 3 个过期封禁已解封
        for uid in (3001, 3002, 3003):
            fields = await _get_ban_fields(store, uid)
            assert fields["is_banned"] == 0, f"user {uid} 应已解封"
            assert fields["ban_expires_at"] is None
        # 未过期临时封禁保持
        fields = await _get_ban_fields(store, 3004)
        assert fields["is_banned"] == 1
        assert fields["ban_expires_at"] is not None
        # 永久封禁保持
        fields = await _get_ban_fields(store, 3005)
        assert fields["is_banned"] == 1
        assert fields["ban_expires_at"] is None

    @pytest.mark.asyncio
    async def test_cleanup_no_expired_returns_zero(self, store):
        """无过期封禁时返回 0。"""
        from services.content_reports import ban_user, cleanup_expired_bans
        await _insert_user(store, 3101)
        await ban_user(user_id=3101, reason="spam", duration_days=1, admin_id=999)
        count = await cleanup_expired_bans()
        assert count == 0


# ════════════════════════════════════════════════════════════════
# P1-11-4: unban_user 清除 ban_expires_at
# ════════════════════════════════════════════════════════════════

class TestUnbanClearsExpiry:
    """unban_user 清除 ban_expires_at。"""

    @pytest.mark.asyncio
    async def test_unban_clears_temp_ban_expires_at(self, store):
        """解封临时封禁后 ban_expires_at 被清空。"""
        from services.content_reports import ban_user, unban_user, is_user_banned
        await _insert_user(store, 4001)
        await ban_user(user_id=4001, reason="spam", duration_days=1, admin_id=999)
        # 确认封禁生效
        assert await is_user_banned(4001) is True
        fields = await _get_ban_fields(store, 4001)
        assert fields["ban_expires_at"] is not None
        # 解封
        ok = await unban_user(user_id=4001, admin_id=999)
        assert ok is True
        fields = await _get_ban_fields(store, 4001)
        assert fields["is_banned"] == 0
        assert fields["ban_expires_at"] is None
        assert await is_user_banned(4001) is False

    @pytest.mark.asyncio
    async def test_unban_clears_permanent_ban(self, store):
        """解封永久封禁后 ban_expires_at 仍为 NULL。"""
        from services.content_reports import ban_user, unban_user, is_user_banned
        await _insert_user(store, 4002)
        await ban_user(user_id=4002, reason="abuse", duration_days=0, admin_id=999)
        ok = await unban_user(user_id=4002, admin_id=999)
        assert ok is True
        fields = await _get_ban_fields(store, 4002)
        assert fields["is_banned"] == 0
        assert fields["ban_expires_at"] is None
        assert await is_user_banned(4002) is False
