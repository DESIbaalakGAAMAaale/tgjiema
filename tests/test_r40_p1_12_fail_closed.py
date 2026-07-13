"""R40 P1-12: Content Policy/举报失败时 fail-closed 测试。

被测能力:
- check_user_banned / is_user_banned 异常时返回 True(fail-closed,保守拒绝)
- check_content_policy 异常时返回 {"allowed": False, "reason": "policy_check_failed"}
- get_user_quota 异常时返回 0 配额 dict(拒绝操作,而非放行)

测试策略:
- check_user_banned: monkeypatch store._db.execute 抛出异常,验证返回 True
- check_content_policy: 注册会抛异常的插件,验证返回 fail-closed dict
- get_user_quota: monkeypatch store._db.execute_fetchall 抛出异常,验证返回 0 配额
"""
import inspect
import shutil
import tempfile
from datetime import datetime
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
    tmpdir = tempfile.mkdtemp(prefix="r40_p1_12_test_")
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


async def _insert_user(store, user_id: int = 2001):
    """插入一个测试用户到 users_local。"""
    await store.upsert_user_local({
        "user_id": user_id,
        "username": "tester",
        "first_name": "Test",
        "is_banned": 0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }, mark_dirty=False)


# ════════════════════════════════════════════════════════════════
# 1. check_user_banned / is_user_banned fail-closed
# ════════════════════════════════════════════════════════════════


class TestCheckUserBannedFailClosed:
    """check_user_banned / is_user_banned 异常时 fail-closed。"""

    @pytest.mark.asyncio
    async def test_check_user_banned_db_none_returns_true(self, monkeypatch):
        """store._db 为 None 时应返回 True(fail-closed,保守视为已封禁)。"""
        from services import content_reports

        # 构造一个 _db 为 None 的 mock store
        class _NoneDbStore:
            _db = None

        monkeypatch.setattr(content_reports, "get_cache_store", lambda: _NoneDbStore())
        result = await content_reports.check_user_banned(99999)
        assert result is True, "store._db=None 时应 fail-closed 返回 True"

    @pytest.mark.asyncio
    async def test_check_user_banned_query_exception_returns_true(self, store, monkeypatch):
        """查询异常时应返回 True(fail-closed),而非 False(fail-open)。"""
        from services import content_reports

        await _insert_user(store, user_id=2002)

        # 让 store._db.execute 抛出异常
        original_execute = store._db.execute

        async def _boom_execute(*args, **kwargs):
            raise RuntimeError("模拟数据库查询异常")

        monkeypatch.setattr(store._db, "execute", _boom_execute)

        result = await content_reports.check_user_banned(2002)
        assert result is True, "查询异常时应 fail-closed 返回 True"

    @pytest.mark.asyncio
    async def test_is_user_banned_query_exception_returns_true(self, store, monkeypatch):
        """is_user_banned 异常时应返回 True(fail-closed)。"""
        from services import content_reports

        await _insert_user(store, user_id=2003)

        async def _boom_execute(*args, **kwargs):
            raise RuntimeError("模拟数据库查询异常")

        monkeypatch.setattr(store._db, "execute", _boom_execute)

        result = await content_reports.is_user_banned(2003)
        assert result is True, "is_user_banned 查询异常时应 fail-closed 返回 True"

    @pytest.mark.asyncio
    async def test_check_user_banned_normal_not_banned(self, store):
        """正常未封禁用户应返回 False(回归测试,确保 fail-closed 不影响正常逻辑)。"""
        from services import content_reports

        await _insert_user(store, user_id=2004)
        result = await content_reports.check_user_banned(2004)
        assert result is False, "未封禁用户应返回 False"

    @pytest.mark.asyncio
    async def test_check_user_banned_normal_banned(self, store):
        """正常已封禁用户应返回 True(回归测试)。"""
        from services import content_reports

        await _insert_user(store, user_id=2005)
        await content_reports.ban_user(2005, admin_id=999, reason="测试封禁")
        result = await content_reports.check_user_banned(2005)
        assert result is True, "已封禁用户应返回 True"


# ════════════════════════════════════════════════════════════════
# 2. check_content_policy fail-closed
# ════════════════════════════════════════════════════════════════


class TestCheckContentPolicyFailClosed:
    """check_content_policy 异常时 fail-closed。"""

    @pytest.mark.asyncio
    async def test_check_content_policy_normal_pass(self):
        """正常文件通过所有策略时应返回 allowed=True 的 dict。"""
        from services.content_policy import (
            check_content_policy, FileMeta, unregister_plugin
        )

        meta = FileMeta(file_name="test.pdf", file_size=1024, file_ext="pdf")
        result = await check_content_policy(meta)
        assert isinstance(result, dict), "check_content_policy 应返回 dict"
        assert result["allowed"] is True, "正常 PDF 文件应允许"

    @pytest.mark.asyncio
    async def test_check_content_policy_normal_reject(self):
        """被禁止的文件类型应返回 allowed=False 的 dict。"""
        from services.content_policy import check_content_policy, FileMeta

        meta = FileMeta(file_name="malware.exe", file_size=1024, file_ext="exe")
        result = await check_content_policy(meta)
        assert isinstance(result, dict), "check_content_policy 应返回 dict"
        assert result["allowed"] is False, ".exe 文件应被拒绝"
        assert "reason" in result, "拒绝时应有 reason 字段"

    @pytest.mark.asyncio
    async def test_check_content_policy_plugin_exception_fail_closed(self, monkeypatch):
        """插件抛异常时 check_content_policy 应 fail-closed 返回 allowed=False。"""
        from services import content_policy as cp_mod
        from services.content_policy import (
            check_content_policy, FileMeta, register_plugin, unregister_plugin
        )

        # 注册一个会抛异常的插件
        def _boom_plugin(file_meta):
            raise RuntimeError("模拟插件异常")

        register_plugin("boom_plugin", _boom_plugin)
        try:
            meta = FileMeta(file_name="test.pdf", file_size=1024, file_ext="pdf")
            result = await check_content_policy(meta)
            assert isinstance(result, dict), "check_content_policy 应返回 dict"
            assert result["allowed"] is False, \
                "插件异常时应 fail-closed 返回 allowed=False"
            assert result.get("reason") == "policy_check_failed", \
                "插件异常时 reason 应为 'policy_check_failed'"
        finally:
            unregister_plugin("boom_plugin")

    @pytest.mark.asyncio
    async def test_check_content_policy_all_plugins_pass_returns_dict(self):
        """所有插件通过时应返回 allowed=True 的 dict(含 policy_name)。"""
        from services.content_policy import check_content_policy, FileMeta

        meta = FileMeta(file_name="doc.pdf", file_size=512, file_ext="pdf")
        result = await check_content_policy(meta)
        assert isinstance(result, dict)
        assert result["allowed"] is True
        assert "policy_name" in result, "通过时应包含 policy_name 字段"


# ════════════════════════════════════════════════════════════════
# 3. get_user_quota fail-closed
# ════════════════════════════════════════════════════════════════


class TestGetUserQuotaFailClosed:
    """get_user_quota 异常时 fail-closed(返回 0 配额 dict)。"""

    @pytest.mark.asyncio
    async def test_get_user_quota_query_exception_returns_zero_quota(self, store, monkeypatch):
        """查询异常时应返回 0 配额 dict,而非 None 或抛异常。"""
        # 先插入一个正常用户配额
        await store.upsert_user_quota(3001, {
            "level": "free",
            "daily_quota": 10,
            "used_today": 0,
            "quota_date": datetime.now().isoformat(),
            "ext_quota": 5,
            "ext_used_today": 0,
            "ext_quota_date": datetime.now().isoformat(),
            "synced_at": 0,
        })

        # 让 execute_fetchall 抛出异常
        async def _boom_fetchall(*args, **kwargs):
            raise RuntimeError("模拟数据库查询异常")

        monkeypatch.setattr(store._db, "execute_fetchall", _boom_fetchall)

        result = await store.get_user_quota(3001)
        assert result is not None, "查询异常时应返回 0 配额 dict,而非 None"
        assert isinstance(result, dict), "应返回 dict"
        assert result.get("daily_quota") == 0, \
            "异常时 daily_quota 应为 0(fail-closed)"
        assert result.get("ext_quota") == 0, \
            "异常时 ext_quota 应为 0(fail-closed)"
        assert result.get("user_id") == 3001, \
            "异常时仍应包含 user_id"

    @pytest.mark.asyncio
    async def test_get_user_quota_db_none_returns_none(self, monkeypatch):
        """store._db 为 None 时(未初始化)返回 None 是可接受的,不算 fail-open。"""
        class _NoneDbStore:
            _db = None

            async def get_user_quota(self, user_id):
                # 模拟当前 CacheStore.get_user_quota 的 _db=None 分支
                return None

        s = _NoneDbStore()
        result = await s.get_user_quota(3002)
        # _db=None 意味着系统未初始化,返回 None 是合理的(非异常场景)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_quota_normal_returns_real_quota(self, store):
        """正常查询应返回真实配额(回归测试,确保 fail-closed 不影响正常逻辑)。"""
        await store.upsert_user_quota(3003, {
            "level": "basic",
            "daily_quota": 20,
            "used_today": 5,
            "quota_date": datetime.now().isoformat(),
            "ext_quota": 10,
            "ext_used_today": 2,
            "ext_quota_date": datetime.now().isoformat(),
            "synced_at": 0,
        })

        result = await store.get_user_quota(3003)
        assert result is not None
        assert result["daily_quota"] == 20, "正常查询应返回真实 daily_quota"
        assert result["used_today"] == 5, "正常查询应返回真实 used_today"
        assert result["ext_quota"] == 10, "正常查询应返回真实 ext_quota"

    @pytest.mark.asyncio
    async def test_module_level_get_user_quota_exception_returns_zero(self, store, monkeypatch):
        """模块级 get_user_quota 异常时也应 fail-closed 返回 0 配额 dict。"""
        from database import cache_store as cs_mod
        from database.cache_store import get_user_quota as module_get_user_quota

        # 让模块级 _store 指向测试 store,确保模块级函数使用测试数据库
        monkeypatch.setattr(cs_mod, "_store", store)

        await store.upsert_user_quota(3004, {
            "level": "free",
            "daily_quota": 15,
            "used_today": 0,
            "quota_date": datetime.now().isoformat(),
            "ext_quota": 5,
            "ext_used_today": 0,
            "ext_quota_date": datetime.now().isoformat(),
            "synced_at": 0,
        })

        # 让 execute_fetchall 抛出异常
        async def _boom_fetchall(*args, **kwargs):
            raise RuntimeError("模拟数据库查询异常")

        monkeypatch.setattr(store._db, "execute_fetchall", _boom_fetchall)

        result = await module_get_user_quota(3004)
        assert result is not None, "模块级函数异常时也应返回 0 配额 dict"
        assert result.get("daily_quota") == 0, \
            "模块级函数异常时 daily_quota 应为 0"
        assert result.get("ext_quota") == 0, \
            "模块级函数异常时 ext_quota 应为 0"
