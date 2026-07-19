"""R40 P1-7: 维护模式 fail-closed + 缓存机制测试。

被测目标:
- ``services.maintenance_mode.is_enabled()`` — fail-closed(抛 MaintenanceCheckError)
- ``services.maintenance_mode.get_maintenance_state()`` — 返回结构化状态(含缓存信息)
- ``services.maintenance_mode.MaintenanceCheckError`` — 异常类
- 内存 + kv_store 持久化缓存(_last_known_enabled / _persist_cache)

测试场景:
1. DB 异常时 is_enabled() 抛 MaintenanceCheckError(fail-closed)
2. DB 未初始化且无缓存时抛 MaintenanceCheckError
3. DB 未初始化但有缓存时使用缓存值(降级)
4. get_maintenance_state() 在 DB 异常时返回缓存信息(source="cache")
5. 高风险入口装饰器在 is_enabled 异常时拒绝请求(fail-closed)

修复说明:
- 原 is_enabled() 在 DB 异常时返回 False(fail-open),
  导致维护模式期间 DB 故障时 Bot 继续接受新请求(数据不一致风险)。
- 修复后:DB 异常时抛 MaintenanceCheckError(fail-closed),
  高风险入口必须捕获此异常并拒绝请求。
- 新增内存 + kv_store 缓存,记录最后已知状态,
  供 get_maintenance_state() 在 DB 异常时返回降级信息。
"""
import asyncio
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
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 _cs_module._store 为测试实例,
    使 get_cache_store() 返回正确的测试 store(非模块级导入时的单例)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r40_p1_7_test_")
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


@pytest_asyncio.fixture
async def reset_cache():
    """每个用例前重置 maintenance_mode 模块级缓存。"""
    from services import maintenance_mode
    maintenance_mode._reset_cache_for_test()
    yield
    maintenance_mode._reset_cache_for_test()


# ════════════════════════════════════════════════════════════════
# P1-7 测试用例
# ════════════════════════════════════════════════════════════════

class TestIsEnabledFailClosed:
    """R40 P1-7: is_enabled() fail-closed 测试。"""

    @pytest.mark.asyncio
    async def test_db_exception_raises_maintenance_check_error(self, real_store, reset_cache):
        """DB 查询异常时 is_enabled() 抛 MaintenanceCheckError(fail-closed)。"""
        from services import maintenance_mode

        # Mock execute_fetchall 抛异常
        with patch.object(
            real_store._db,
            "execute_fetchall",
            new=AsyncMock(side_effect=Exception("SQLite 磁盘 I/O 错误")),
        ):
            with pytest.raises(maintenance_mode.MaintenanceCheckError) as exc_info:
                await maintenance_mode.is_enabled()

            assert "数据库查询异常" in str(exc_info.value), \
                f"异常消息应包含'数据库查询异常',实际: {exc_info.value}"

    @pytest.mark.asyncio
    async def test_db_not_initialized_no_cache_raises(self, real_store, reset_cache):
        """DB 未初始化且无缓存时抛 MaintenanceCheckError。"""
        from services import maintenance_mode

        # 确保缓存为空
        maintenance_mode._reset_cache_for_test()
        assert maintenance_mode._last_known_enabled is None

        # Mock store._db 为 None(模拟 DB 未初始化)
        with patch.object(real_store, "_db", new=None):
            with patch(
                "services.maintenance_mode.get_cache_store",
                return_value=real_store,
            ):
                with pytest.raises(maintenance_mode.MaintenanceCheckError) as exc_info:
                    await maintenance_mode.is_enabled()

                assert "数据库未初始化" in str(exc_info.value), \
                    f"异常消息应包含'数据库未初始化',实际: {exc_info.value}"

    @pytest.mark.asyncio
    async def test_db_not_initialized_with_cache_returns_cache(self, real_store, reset_cache):
        """DB 未初始化但有缓存时返回缓存值(降级模式)。"""
        from services import maintenance_mode

        # 先开启维护模式(写入缓存)
        await maintenance_mode.enable("预设置缓存", started_by=100)
        # 验证缓存已写入
        assert maintenance_mode._last_known_enabled is True

        # Mock store._db 为 None(模拟 DB 故障)
        with patch.object(real_store, "_db", new=None):
            with patch(
                "services.maintenance_mode.get_cache_store",
                return_value=real_store,
            ):
                result = await maintenance_mode.is_enabled()
                # DB 不可用但有缓存(True) → 返回缓存值
                assert result is True, \
                    "DB 未初始化但有缓存(True)时应返回缓存值 True"

    @pytest.mark.asyncio
    async def test_is_enabled_updates_cache_on_success(self, real_store, reset_cache):
        """is_enabled() 成功查询后更新缓存。"""
        from services import maintenance_mode

        # 初始缓存为空
        assert maintenance_mode._last_known_enabled is None

        # 查询(无 maintenance_state 记录 → False)
        result = await maintenance_mode.is_enabled()
        assert result is False

        # 验证缓存已更新
        assert maintenance_mode._last_known_enabled is False, \
            "is_enabled() 成功后应更新缓存"

    @pytest.mark.asyncio
    async def test_is_enabled_returns_true_when_enabled(self, real_store, reset_cache):
        """维护模式开启时 is_enabled() 返回 True。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试开启", started_by=100)

        # 重置缓存后查询(验证从 DB 读取)
        maintenance_mode._reset_cache_for_test()
        result = await maintenance_mode.is_enabled()
        assert result is True

        # 验证缓存已更新
        assert maintenance_mode._last_known_enabled is True


class TestGetMaintenanceState:
    """R40 P1-7: get_maintenance_state() 返回结构化状态。"""

    @pytest.mark.asyncio
    async def test_returns_dict_with_required_fields(self, real_store, reset_cache):
        """get_maintenance_state() 返回包含必需字段的字典。"""
        from services import maintenance_mode

        state = await maintenance_mode.get_maintenance_state()

        assert isinstance(state, dict)
        assert "enabled" in state
        assert "last_checked" in state
        assert "last_known" in state
        assert "error" in state
        assert "source" in state

    @pytest.mark.asyncio
    async def test_db_exception_returns_cache_source(self, real_store, reset_cache):
        """DB 异常时 get_maintenance_state() 返回 source="cache"。"""
        from services import maintenance_mode

        # 先设置缓存为 True(开启维护模式)
        await maintenance_mode.enable("设置缓存", started_by=100)
        assert maintenance_mode._last_known_enabled is True

        # Mock DB 异常
        with patch.object(
            real_store._db,
            "execute_fetchall",
            new=AsyncMock(side_effect=Exception("DB 故障")),
        ):
            state = await maintenance_mode.get_maintenance_state()

        assert state["source"] == "cache", \
            f"DB 异常时 source 应为 'cache',实际: {state['source']}"
        assert state["last_known"] is True, \
            "DB 异常时应返回缓存值 True"
        assert state["enabled"] is True, \
            "DB 异常时 enabled 应使用缓存值 True"
        assert "DB 故障" in state["error"], \
            f"error 应包含异常信息,实际: {state['error']}"

    @pytest.mark.asyncio
    async def test_db_success_returns_db_source(self, real_store, reset_cache):
        """DB 正常时 get_maintenance_state() 返回 source="db"。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 DB source", started_by=100)

        state = await maintenance_mode.get_maintenance_state()
        assert state["source"] == "db"
        assert state["enabled"] is True
        assert state["error"] == ""

    @pytest.mark.asyncio
    async def test_no_cache_returns_unknown_source(self, real_store, reset_cache):
        """DB 异常且无缓存时 source="unknown"。"""
        from services import maintenance_mode

        # 确保缓存为空
        maintenance_mode._reset_cache_for_test()
        assert maintenance_mode._last_known_enabled is None

        # Mock DB 异常
        with patch.object(
            real_store._db,
            "execute_fetchall",
            new=AsyncMock(side_effect=Exception("DB 故障")),
        ):
            state = await maintenance_mode.get_maintenance_state()

        assert state["source"] == "unknown", \
            f"无缓存时 source 应为 'unknown',实际: {state['source']}"
        assert state["last_known"] is None
        assert state["enabled"] is None


class TestMaintenanceCachePersistence:
    """R40 P1-7: 缓存持久化(kv_store)测试。"""

    @pytest.mark.asyncio
    async def test_enable_persists_cache_to_kv_store(self, real_store, reset_cache):
        """enable() 后缓存写入 kv_store(跨进程共享)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试持久化", started_by=100)

        # 验证 kv_store 中有缓存记录
        rows = await real_store._db.execute_fetchall(
            "SELECT value FROM kv_store WHERE key = ?",
            (maintenance_mode._KV_LAST_KNOWN_KEY,),
        )
        assert rows and rows[0], "kv_store 应有 last_known 缓存记录"
        assert rows[0][0] == "1", f"缓存值应为 '1'(True),实际: {rows[0][0]}"

    @pytest.mark.asyncio
    async def test_disable_persists_cache_to_kv_store(self, real_store, reset_cache):
        """disable() 后缓存写入 kv_store(False)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("先开启", started_by=100)

        # 清理 enable 产生的 dirty_outbox(模拟 drain_queues 已排空)
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        await maintenance_mode.disable(ended_by=100)

        rows = await real_store._db.execute_fetchall(
            "SELECT value FROM kv_store WHERE key = ?",
            (maintenance_mode._KV_LAST_KNOWN_KEY,),
        )
        assert rows and rows[0]
        assert rows[0][0] == "0", f"缓存值应为 '0'(False),实际: {rows[0][0]}"

    @pytest.mark.asyncio
    async def test_load_persisted_cache_restores_state(self, real_store, reset_cache):
        """_load_persisted_cache() 从 kv_store 恢复缓存到内存。"""
        from services import maintenance_mode

        # 写入缓存(True)
        await maintenance_mode.enable("设置缓存", started_by=100)
        assert maintenance_mode._last_known_enabled is True

        # 清空内存缓存(模拟进程重启)
        maintenance_mode._reset_cache_for_test()
        assert maintenance_mode._last_known_enabled is None

        # 从 kv_store 加载
        await maintenance_mode._load_persisted_cache()

        # 验证:内存缓存已恢复
        assert maintenance_mode._last_known_enabled is True, \
            "_load_persisted_cache 应从 kv_store 恢复缓存值 True"


class TestRequireMaintenanceCheckFailClosed:
    """R40 P1-7: 装饰器在 is_enabled 异常时 fail-closed(拒绝请求)。"""

    @pytest.mark.asyncio
    async def test_decorator_rejects_on_maintenance_check_error(self, reset_cache):
        """is_enabled 抛 MaintenanceCheckError 时装饰器拒绝请求(fail-closed)。"""
        from services import maintenance_mode

        # 创建一个测试用的被装饰函数
        @maintenance_mode.require_maintenance_check(action="测试操作")
        async def test_handler(update, context):
            return "EXECUTED"

        # Mock update 对象
        mock_update = MagicMock()

        # R65 P1-01: maintenance_mode 已迁移为 safe_reply_text typed adapter,
        # mock safe_reply_text 避免依赖真实 telegram(isinstance(Message) 在 mock 下报错)
        mock_safe_reply = AsyncMock()
        # Mock is_enabled 抛 MaintenanceCheckError
        with patch.object(
            maintenance_mode,
            "is_enabled",
            new=AsyncMock(side_effect=maintenance_mode.MaintenanceCheckError("DB 故障")),
        ), patch.object(maintenance_mode, "safe_reply_text", new=mock_safe_reply):
            result = await test_handler(mock_update, context=None)

        # 验证:返回 None(未执行原函数)
        assert result is None, "fail-closed 时应返回 None(不执行原函数)"

        # 验证:回复了"服务暂不可用"
        mock_safe_reply.assert_called_once()
        payload = mock_safe_reply.call_args[0][1]
        text = payload.render(None)  # from_raw_text 构造,_raw_text 已设置
        assert "服务暂不可用" in text, \
            f"应回复'服务暂不可用',实际: {text}"

    @pytest.mark.asyncio
    async def test_decorator_rejects_on_generic_exception(self, reset_cache):
        """is_enabled 抛其他异常时装饰器也 fail-closed(拒绝请求)。"""
        from services import maintenance_mode

        @maintenance_mode.require_maintenance_check(action="测试操作")
        async def test_handler(update, context):
            return "EXECUTED"

        mock_update = MagicMock()

        # R65 P1-01: maintenance_mode 已迁移为 safe_reply_text typed adapter
        mock_safe_reply = AsyncMock()
        # Mock is_enabled 抛通用异常
        with patch.object(
            maintenance_mode,
            "is_enabled",
            new=AsyncMock(side_effect=RuntimeError("未知错误")),
        ), patch.object(maintenance_mode, "safe_reply_text", new=mock_safe_reply):
            result = await test_handler(mock_update, context=None)

        assert result is None
        mock_safe_reply.assert_called_once()
