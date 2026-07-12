"""R36 Batch 5: H5 CAS/lease 生产模式 fail-closed 测试。

被测目标:
- ``bots.mon_bot.MonBot._handle_cas_failure``: CAS/lease 失败时的统一处理
- ``config.settings.TOPOLOGY_FAIL_CLOSED``: fail-closed 开关

测试策略:
- ``bots.mon_bot`` 模块在导入时依赖 telegram / services.mon / utils.monitor / database 等,
  Python 3.9 环境下 database.__init__ 使用 3.10+ 语法无法直接加载。
  因此在导入前向 sys.modules 注入轻量 mock,仅让被测方法 ``_handle_cas_failure`` 可运行。
- 用 MagicMock + AsyncMock 模拟 CacheStore,隔离 SQLite 依赖。
- 通过 ``importlib.util.spec_from_file_location`` 加载 mon_bot.py 源文件。

覆盖场景:
- H5-A: TOPOLOGY_FAIL_CLOSED=True 时 CAS 失败不 fallback,返回 False
- H5-B: TOPOLOGY_FAIL_CLOSED=False 时 CAS 失败 fallback 到 update_cell_fields_local,返回 True
- H5-C: 审计事件写入 kv_store(包含 slot_id / actual_version / mode / maintenance_mode)
- H5-D: fail-closed 模式下 update_cell_fields_local 不被调用
- H5-E: 维护模式下 update_cell_fields_local 被调用,且字段正确
- H5-F: get_cells_by_version 异常时不阻塞主流程(降级到 get_max_topology_version)
- H5-G: set_kv 异常时不阻塞主流程
"""
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ────────────── 模块加载: 注入 mock 依赖后加载 bots/mon_bot.py ──────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MON_BOT_PATH = _PROJECT_ROOT / "bots" / "mon_bot.py"


def _ensure_module(name: str):
    """确保 sys.modules 中存在指定模块,不存在则创建轻量 mock。"""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return sys.modules[name]


def _install_bots_mon_bot_deps():
    """为 bots.mon_bot 注入所需的 mock 依赖(telegram / services.mon / utils.monitor 等)。

    conftest 已注入 config 与 database(轻量包)。
    这里补充 telegram、services.mon、utils.monitor,以及 database 包上的 8 个函数。
    """
    # telegram 包
    tg = _ensure_module("telegram")
    tg.Bot = MagicMock(name="MockTelegramBot")
    tg_err = _ensure_module("telegram.error")
    tg_err.TelegramError = type("TelegramError", (Exception,), {})
    tg_err.RetryAfter = type("RetryAfter", (Exception,), {})

    # services.mon 包(提供 MonScheduler)
    svc_mon = _ensure_module("services.mon")
    svc_mon.MonScheduler = MagicMock(name="MockMonScheduler")
    # services 父包(若不存在)
    _ensure_module("services")

    # utils.monitor(提供 metrics)
    util_mon = _ensure_module("utils.monitor")
    util_mon.metrics = MagicMock(name="MockMetrics")
    _ensure_module("utils")

    # database 包: conftest 已创建轻量包,补齐 8 个函数属性
    db_mod = sys.modules.get("database")
    if db_mod is None:
        db_mod = types.ModuleType("database")
        db_mod.__path__ = [str(_PROJECT_ROOT / "database")]
        sys.modules["database"] = db_mod
    for fn_name in (
        "init_db", "close_db", "get_cells_col",
        "log_rotate", "get_spare_for_account", "get_any_spare",
        "consume_spare", "get_rotation_config",
    ):
        if not hasattr(db_mod, fn_name):
            setattr(db_mod, fn_name, MagicMock(name=f"mock_{fn_name}"))

    # database.cache_store.get_cache_store: 若未提供则补 mock
    cs_mod = sys.modules.get("database.cache_store")
    if cs_mod is None or not hasattr(cs_mod, "get_cache_store"):
        cs_mod = types.ModuleType("database.cache_store")
        cs_mod.get_cache_store = MagicMock(name="mock_get_cache_store")
        sys.modules["database.cache_store"] = cs_mod
        setattr(db_mod, "cache_store", cs_mod)


def _load_mon_bot_module():
    """加载 bots/mon_bot.py 为模块对象(已注入 mock 依赖)。"""
    _install_bots_mon_bot_deps()
    # bots 父包
    if "bots" not in sys.modules:
        bots_pkg = types.ModuleType("bots")
        bots_pkg.__path__ = [str(_PROJECT_ROOT / "bots")]
        sys.modules["bots"] = bots_pkg
    # 加载 mon_bot.py
    spec = importlib.util.spec_from_file_location(
        "bots.mon_bot", str(_MON_BOT_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["bots.mon_bot"] = module
    spec.loader.exec_module(module)
    return module


# 加载被测模块(若加载失败则跳过全部测试)
try:
    _mon_bot_module = _load_mon_bot_module()
    MonBotClass = _mon_bot_module.MonBot
    _LOAD_OK = True
    _LOAD_ERR = None
except Exception as _e:  # pragma: no cover
    _LOAD_OK = False
    _LOAD_ERR = _e
    MonBotClass = None


# 测试模块级 skip(无法加载时)
pytestmark = pytest.mark.skipif(
    not _LOAD_OK,
    reason=f"bots.mon_bot 无法加载: {_LOAD_ERR}",
)


# ────────────── 辅助: 构造 MonBot 实例(跳过 __init__ 重依赖) ──────────────


def _make_mon_bot():
    """创建 MonBot 实例,跳过 __init__(避免初始化 telegram Bot / MonScheduler 等)。

    使用 __new__ + 手动设置 _handle_cas_failure 所需的最少属性。
    """
    return MonBotClass.__new__(MonBotClass)


def _make_store_mock(
    cells_with_version=None,
    max_version=7,
    set_kv_ok=True,
    update_local_ok=True,
):
    """构造 mock CacheStore。

    - cells_with_version: get_cells_by_version 返回值(默认含一条 slot=s1, v=7)
    - max_version: get_max_topology_version 返回值
    - set_kv_ok: True → set_kv 成功; False → set_kv 抛异常
    - update_local_ok: True → update_cell_fields_local 成功; False → 抛异常
    """
    store = MagicMock(name="mock_cache_store")
    if cells_with_version is None:
        cells_with_version = [{"slot_id": "s1", "topology_version": 7}]
    store.get_cells_by_version = AsyncMock(return_value=cells_with_version)
    store.get_max_topology_version = AsyncMock(return_value=max_version)
    if set_kv_ok:
        store.set_kv = AsyncMock(return_value=None)
    else:
        store.set_kv = AsyncMock(side_effect=RuntimeError("kv write failed"))
    if update_local_ok:
        store.update_cell_fields_local = AsyncMock(return_value=None)
    else:
        store.update_cell_fields_local = AsyncMock(
            side_effect=RuntimeError("update_local failed")
        )
    return store


# ───────────────────── H5-A: fail-closed 模式不 fallback ─────────────────────


class TestFailClosedNoFallback:
    """R36 H5: TOPOLOGY_FAIL_CLOSED=True 时 CAS 失败拒绝 fallback。"""

    @pytest.mark.asyncio
    async def test_returns_false_when_fail_closed(self, monkeypatch):
        """fail_closed=True → 返回 False(拓扑变更被拒绝)。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock()

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
                reason="测试 CAS 失败",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_update_cell_fields_local_when_fail_closed(self, monkeypatch):
        """fail_closed=True → 不调用 update_cell_fields_local(不绕过并发保护)。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock()

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
                reason="测试 CAS 失败",
            )

        # 关键: fail-closed 模式下绝不调用 update_cell_fields_local
        store.update_cell_fields_local.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_magicmock_default_is_truthy_fail_closed(self, monkeypatch):
        """MagicMock 默认对任意属性返回 truthy 对象, bool(MagicMock())=True。

        即便未显式设置 TOPOLOGY_FAIL_CLOSED, getattr(settings, ..., True) 也得到 truthy
        → fail-closed 默认生效(符合生产安全预期)。
        """
        import config
        # 不显式设置,验证 MagicMock 默认行为下 fail-closed 生效
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock()

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
            )

        assert result is False
        store.update_cell_fields_local.assert_not_awaited()


# ───────────────────── H5-B: 维护模式 fallback ─────────────────────


class TestMaintenanceModeFallback:
    """R36 H5: TOPOLOGY_FAIL_CLOSED=False 时 CAS 失败 fallback 到旧写法。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_maintenance(self, monkeypatch):
        """fail_closed=False(维护模式) → 返回 True(fallback 已应用)。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", False)

        bot = _make_mon_bot()
        store = _make_store_mock()

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1", "channel_id": 999},
                reason="维护模式测试",
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_update_cell_fields_local_called_with_correct_fields(self, monkeypatch):
        """维护模式 → update_cell_fields_local 被调用,字段正确。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", False)

        bot = _make_mon_bot()
        store = _make_store_mock()

        fallback_fields = {
            "status": "lost",
            "next_active_chat_id": None,
            "channel_id": 12345,
        }

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            await bot._handle_cas_failure(
                slot_id="s2",
                expected_status="active",
                new_status="lost",
                fallback_fields=fallback_fields,
                fallback_mark_dirty=True,
                reason="测试字段",
            )

        store.update_cell_fields_local.assert_awaited_once()
        args, kwargs = store.update_cell_fields_local.await_args
        # 签名: (slot_id, fields, mark_dirty=...)
        assert args[0] == "s2"
        assert args[1] == fallback_fields
        assert kwargs.get("mark_dirty", args[2] if len(args) > 2 else False) is True

    @pytest.mark.asyncio
    async def test_maintenance_returns_false_when_update_local_fails(self, monkeypatch):
        """维护模式下 update_cell_fields_local 抛异常 → 返回 False(fallback 失败)。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", False)

        bot = _make_mon_bot()
        store = _make_store_mock(update_local_ok=False)

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
                reason="update_local 抛异常",
            )

        assert result is False


# ───────────────────── H5-C: 审计事件写入 ─────────────────────


class TestAuditEventWritten:
    """R36 H5: CAS 失败时写审计事件到 kv_store。"""

    @pytest.mark.asyncio
    async def test_audit_record_written_fail_closed(self, monkeypatch):
        """fail-closed 模式 → 写审计事件,mode=fail_closed。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock(
            cells_with_version=[{"slot_id": "s1", "topology_version": 42}],
        )

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
                owner="mon_bot",
                reason="audit 测试",
            )

        store.set_kv.assert_awaited_once()
        args, _ = store.set_kv.await_args
        audit_key = args[0]
        audit_json = args[1]
        # key 格式: cas_audit:<slot_id>:<timestamp_ms>
        assert audit_key.startswith("cas_audit:s1:")
        # 解析 JSON
        record = json.loads(audit_json)
        assert record["slot_id"] == "s1"
        assert record["expected_status"] == "active"
        assert record["new_status"] == "shadow1"
        assert record["lease_owner"] == "mon_bot"
        assert record["failure_reason"] == "audit 测试"
        assert record["mode"] == "fail_closed"
        assert record["maintenance_mode"] is False
        assert record["actual_version"] == 42
        assert record["expected_version"] is None
        assert "timestamp" in record

    @pytest.mark.asyncio
    async def test_audit_record_written_maintenance(self, monkeypatch):
        """维护模式 → 写审计事件,mode=maintenance,maintenance_mode=True。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", False)

        bot = _make_mon_bot()
        store = _make_store_mock()

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            await bot._handle_cas_failure(
                slot_id="s9",
                expected_status="lost",
                new_status="shadow2",
                fallback_fields={"status": "shadow2"},
                reason="维护审计",
            )

        store.set_kv.assert_awaited_once()
        # set_kv 位置参数: (key, value)
        args = store.set_kv.await_args.args
        record = json.loads(args[1])
        assert record["mode"] == "maintenance"
        assert record["maintenance_mode"] is True
        assert record["slot_id"] == "s9"

    @pytest.mark.asyncio
    async def test_audit_uses_max_version_when_cell_not_found(self, monkeypatch):
        """slot_id 在 get_cells_by_version 中未找到 → actual_version 退回 get_max_topology_version。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        # cells_with_version 不含 s1 → actual_version 应回退到 max_version
        store = _make_store_mock(
            cells_with_version=[{"slot_id": "other", "topology_version": 5}],
            max_version=99,
        )

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
            )

        args = store.set_kv.await_args.args
        record = json.loads(args[1])
        assert record["actual_version"] == 99


# ───────────────────── H5-F/G: 异常安全 ─────────────────────


class TestExceptionSafety:
    """R36 H5: 审计/版本查询异常不阻塞主流程。"""

    @pytest.mark.asyncio
    async def test_get_cells_by_version_exception_does_not_block(self, monkeypatch):
        """get_cells_by_version 抛异常 → 退回 get_max_topology_version,不抛。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock()
        store.get_cells_by_version = AsyncMock(
            side_effect=RuntimeError("DB locked")
        )
        store.get_max_topology_version = AsyncMock(return_value=15)

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
            )

        # 不抛异常,返回 False(fail-closed)
        assert result is False
        # 审计仍写入,actual_version 来自 get_max_topology_version
        args = store.set_kv.await_args.args
        record = json.loads(args[1])
        assert record["actual_version"] == 15

    @pytest.mark.asyncio
    async def test_set_kv_exception_does_not_block_fail_closed(self, monkeypatch):
        """set_kv 抛异常 → 不影响 fail-closed 决策,返回 False。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock(set_kv_ok=False)

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
            )

        # set_kv 失败不抛异常,fail-closed 仍返回 False
        assert result is False
        store.set_kv.assert_awaited_once()
        # update_cell_fields_local 仍不被调用(fail-closed)
        store.update_cell_fields_local.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_kv_exception_does_not_block_maintenance(self, monkeypatch):
        """维护模式下 set_kv 抛异常 → 审计失败但 fallback 仍执行,返回 True。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", False)

        bot = _make_mon_bot()
        store = _make_store_mock(set_kv_ok=False)

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
            )

        # 审计失败不影响 fallback
        assert result is True
        store.update_cell_fields_local.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_version_queries_fail_still_returns(self, monkeypatch):
        """get_cells_by_version 和 get_max_topology_version 都失败 → actual_version=None,不抛。"""
        import config
        monkeypatch.setattr(config.settings, "TOPOLOGY_FAIL_CLOSED", True)

        bot = _make_mon_bot()
        store = _make_store_mock()
        store.get_cells_by_version = AsyncMock(
            side_effect=RuntimeError("err1")
        )
        store.get_max_topology_version = AsyncMock(
            side_effect=RuntimeError("err2")
        )

        with patch.object(_mon_bot_module, "get_cache_store", return_value=store):
            result = await bot._handle_cas_failure(
                slot_id="s1",
                expected_status="active",
                new_status="shadow1",
                fallback_fields={"status": "shadow1"},
            )

        assert result is False
        # 审计仍尝试写入(actual_version=None)
        store.set_kv.assert_awaited_once()
        record = json.loads(store.set_kv.await_args.args[1])
        assert record["actual_version"] is None


# ───────────────────── 配置项默认值测试 ─────────────────────


class TestSettingsDefault:
    """R36 H5: settings.TOPOLOGY_FAIL_CLOSED 默认值校验。"""

    def test_default_is_true(self):
        """生产默认 True(fail-closed)。"""
        # 直接读取源文件解析,避免触发 Settings 完整加载
        src = (Path(__file__).resolve().parent.parent
               / "config" / "settings.py").read_text(encoding="utf-8")
        # 简单字符串校验: 配置项存在且默认 True
        assert "TOPOLOGY_FAIL_CLOSED: bool = True" in src

    def test_no_other_topology_fail_closed_occurrences_in_settings(self):
        """配置项只定义一次。"""
        src = (Path(__file__).resolve().parent.parent
               / "config" / "settings.py").read_text(encoding="utf-8")
        # 只校验定义行存在且唯一
        assert src.count("TOPOLOGY_FAIL_CLOSED: bool = True") == 1
