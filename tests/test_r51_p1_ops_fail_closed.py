"""R51 P1-6 / P1-7: 运维 fail-closed 测试。

被测目标:
    - P1-6: ``services/maintenance_mode.py`` 维护模式 fail-closed
        * ``execute_maintenance_workflow`` 失败时 recover_status 持久化失败 → 严重告警 + fail-closed
        * ``disable`` 在 recover_status='pending' 时必须绑定 request_hash + principal + approval_action_id
        * ``recover_maintenance`` 强制要求 request_hash + approval_action_id + RBAC 权限
    - P1-7: ``services/prometheus_exporter.py`` + ``services/ru_cost_center.py`` 指标完善
        * 高基数 label:CI 模式 raise AppError,运行时丢弃违规行
        * PEL/dlq_depth/i18n missing-key 采集失败输出 ``collector_success=0``
        * ``ru_cost_center`` 估算值标记 ``ru_estimated=1``,官方值标记 ``ru_estimated=0``
        * Prometheus ``tgjiema_ru_daily_usage`` 指标包含 ``ru_estimated`` label

测试场景(13 个用例):
 1. workflow 失败 + recover_status 持久化失败 → recover_status_persist_failed=True + fail-closed
 2. disable 在 recover_status=pending + 有 approval_action_id 但无 request_hash → 拒绝
 3. recover_maintenance 无 request_hash → PermissionError(协议化错误 MAINTENANCE_RECOVER_BINDING_REQUIRED)
 4. recover_maintenance 无 approval_action_id → PermissionError
 5. recover_maintenance 全部绑定通过 → 成功关闭(fail-open 仅在审批通过时)
 6. 高基数 label CI 模式 raise AppError(METRICS_HIGH_CARDINALITY_LABEL)
 7. 高基数 label 运行时模式丢弃违规行(不输出)
 8. PEL 采集失败输出 collector_success=0
 9. dlq_depth 采集失败输出 collector_success=0
10. i18n missing-key 采集失败输出 collector_success=0
11. ru_cost_center record_usage 默认标记 ru_estimated=1(估算值)
12. ru_cost_center record_official_usage 标记 ru_estimated=0(官方值)
13. Prometheus tgjiema_ru_daily_usage 包含 ru_estimated label

测试策略:
    - Maintenance 测试:使用真实 SQLite 临时数据库(隔离生产数据)
    - Prometheus 测试:使用 monkeypatch + 内存 mock,不依赖真实 SQLite / R2 / CRDB
    - AppError + ErrorCodes 协议化错误校验
    - 中文注释,英文 raise 消息
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(用于 Maintenance 测试)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r51_p1_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
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


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存,避免跨用例污染。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_command_execution(
    store,
    action_id: str,
    status: str = "approved",
    request_hash: str = "test_hash_001",
    result_json: str = '{"success": true}',
):
    """直接插入一条 command_executions 记录(模拟 CommandBus 审批结果)。

    R52 P0-5: 状态机统一为 pending → approved → executing → executed/failed,
    审批通过后执行前的状态为 'approved'(旧版 'executed' 语义冲突已废弃)。
    """
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        "INSERT INTO command_executions "
        "(action_id, command_type, principal_id, status, owner, lease_until, "
        " request_hash, result, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (action_id, "recover_maintenance", 100, status, "test_worker",
         request_hash, result_json, now, now),
    )
    await store._db.commit()


async def _set_recover_status(store, status: str):
    """直接 UPDATE maintenance_state.recover_status(用于场景构造)。"""
    await store._db.execute(
        "UPDATE maintenance_state SET recover_status = ? WHERE id = ?",
        (status, 1),
    )
    await store._db.commit()


async def _get_recover_status(store) -> str:
    """查询 maintenance_state.recover_status。"""
    rows = await store._db.execute_fetchall(
        "SELECT recover_status FROM maintenance_state WHERE id = ?", (1,)
    )
    if rows and rows[0]:
        return rows[0][0] or "completed"
    return "completed"


# ════════════════════════════════════════════════════════════════
# P1-6 测试:Maintenance fail-closed
# ════════════════════════════════════════════════════════════════

class TestMaintenanceRecoverStatusPersistFailClosed:
    """R51 P1-6: 维护工作流失败时 recover_status 持久化失败必须 fail-closed。"""

    @pytest.mark.asyncio
    async def test_workflow_persist_failure_returns_fail_closed_flag(
        self, real_store, reset_cache
    ):
        """workflow 失败 + recover_status 持久化失败 → recover_status_persist_failed=True。

        场景:
            1. drain_queues 失败(workflow 整体失败)
            2. cache_store.transaction() 抛异常(recover_status 持久化失败)
        预期:
            - result["success"] is False
            - result["maintenance_kept_enabled"] is True(fail-closed)
            - result["recover_status_persist_failed"] is True(R51 P1-6 标记)
            - result["recover_persist_error"] 非空(包含异常信息)
        """
        from services import maintenance_mode

        # Mock drain_queues 失败(触发 workflow 失败路径)
        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": False,
                "remaining_outbox": 5,
                "remaining_jobs": 0,
                "timeout": True,
            }),
        ), patch.object(
            real_store,
            "transaction",
            side_effect=RuntimeError("SQLite 锁竞争(transaction 不可用)"),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试 recover_status 持久化失败 → fail-closed",
                started_by=100,
                auto_disable=True,
            )

        # workflow 失败
        assert result["success"] is False, "drain 失败时 workflow 应失败"
        # fail-closed:保持 maintenance enabled
        assert result["maintenance_kept_enabled"] is True, \
            "workflow 失败时应保持 maintenance enabled(fail-closed)"
        # R51 P1-6:recover_status 持久化失败标记
        assert result.get("recover_status_persist_failed") is True, \
            "recover_status 持久化失败时应设置 recover_status_persist_failed=True"
        # 持久化错误信息非空
        persist_err = result.get("recover_persist_error", "")
        assert persist_err, \
            "recover_persist_error 应包含异常信息"
        assert "锁竞争" in persist_err or "transaction" in persist_err, \
            f"recover_persist_error 应包含异常信息,实际: {persist_err}"


class TestDisableRequiresRequestHashWhenRecoverPending:
    """R51 P1-6: disable 在 recover_status='pending' 时必须绑定 request_hash。"""

    @pytest.mark.asyncio
    async def test_disable_rejected_when_pending_without_request_hash(
        self, real_store, reset_cache
    ):
        """recover_status=pending + approval_action_id 但无 request_hash → 拒绝。

        R51 P1-6:即使提供了 approval_action_id,若缺少 request_hash 仍拒绝关闭,
        确保审批动作与请求来源可审计追溯。
        """
        from services import maintenance_mode

        # 开启维护模式 + 设置 recover_status='pending'
        await maintenance_mode.enable("测试 R51 P1-6 request_hash 校验", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 清理 dirty_outbox,让常规前置检查能通过
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 插入一条 command_executions 记录(status='approved')
        approval_action_id = "recover_action_r51_001"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        # 调用 disable(有 approval_action_id 但无 request_hash)应抛异常
        with pytest.raises(maintenance_mode.MaintenancePreconditionError) as exc_info:
            await maintenance_mode.disable(
                ended_by=100,
                approval_action_id=approval_action_id,
                # 故意不传 request_hash
            )

        # 异常消息应提及 request_hash(R51 P1-6)
        assert "request_hash" in str(exc_info.value).lower(), \
            f"异常消息应提及 request_hash,实际: {exc_info.value}"

        # maintenance_state 仍为 enabled(未关闭)
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, \
            "recover_status=pending + 无 request_hash 时 disable 应拒绝"
        # recover_status 仍为 pending
        assert await _get_recover_status(real_store) == "pending"


class TestRecoverMaintenanceBindingRequired:
    """R51 P1-6: recover_maintenance 强制 request_hash + approval_action_id 绑定。"""

    @pytest.mark.asyncio
    async def test_recover_maintenance_rejected_without_request_hash(
        self, real_store, reset_cache
    ):
        """recover_maintenance 无 request_hash → PermissionError。

        R51 P1-6:即使提供了 approval_action_id,若缺少 request_hash 仍拒绝,
        抛 PermissionError 并写 audit_log(MAINTENANCE_RECOVER_BINDING_REQUIRED)。
        """
        from services import maintenance_mode

        await maintenance_mode.enable("测试 recover_maintenance 无 request_hash", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 插入一条 approved 状态的审批记录
        approval_action_id = "recover_action_r51_002"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
        )

        # 调用 recover_maintenance 无 request_hash → PermissionError
        with pytest.raises(PermissionError) as exc_info:
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="测试无 request_hash 拒绝",
                approval_action_id=approval_action_id,
                # 故意不传 request_hash
            )

        # 异常消息应提及 request_hash(R51 P1-6)
        assert "request_hash" in str(exc_info.value).lower(), \
            f"异常消息应提及 request_hash,实际: {exc_info.value}"

        # maintenance_state 仍为 enabled(未关闭)
        enabled = await maintenance_mode.is_enabled()
        assert enabled is True, \
            "recover_maintenance 无 request_hash 时应拒绝关闭"

    @pytest.mark.asyncio
    async def test_recover_maintenance_rejected_without_approval_action_id(
        self, real_store, reset_cache
    ):
        """recover_maintenance 无 approval_action_id → PermissionError(向后兼容 R42 P1-12)。"""
        from services import maintenance_mode

        await maintenance_mode.enable("测试 recover_maintenance 无 approval", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 调用 recover_maintenance 无 approval_action_id → PermissionError
        with pytest.raises(PermissionError) as exc_info:
            await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="测试无 approval 拒绝",
                # 故意不传 approval_action_id 和 request_hash
            )

        assert "approval_action_id" in str(exc_info.value).lower(), \
            f"异常消息应提及 approval_action_id,实际: {exc_info.value}"

    @pytest.mark.asyncio
    async def test_recover_maintenance_success_with_all_bindings(
        self, real_store, reset_cache
    ):
        """recover_maintenance 全部绑定通过(request_hash + approval + RBAC) → 成功关闭。

        R51 P1-6:提供 request_hash + approval_action_id + principal 拥有权限时,
        允许关闭维护模式(fail-open 仅在审批通过时)。
        """
        from services import maintenance_mode

        await maintenance_mode.enable("测试 recover_maintenance 成功", started_by=100)
        await _set_recover_status(real_store, "pending")

        # 清理 dirty_outbox
        await real_store._db.execute("UPDATE dirty_outbox SET processed = 1")
        await real_store._db.commit()

        # 插入一条 approved 状态的审批记录
        approval_action_id = "recover_action_r51_003"
        request_hash = "r51_test_hash_003"
        await _insert_command_execution(
            real_store, approval_action_id, status="approved",
            request_hash=request_hash,
        )

        # Mock RBAC 权限校验通过(principal=100 拥有 maintenance:recover 权限)
        with patch(
            "services.rbac.check_permission",
            new=AsyncMock(return_value=True),
        ):
            result = await maintenance_mode.recover_maintenance(
                principal_id=100,
                reason="测试全部绑定通过",
                approval_action_id=approval_action_id,
                request_hash=request_hash,
            )

        assert result is True, \
            "全部绑定通过(request_hash + approval + RBAC)时 recover_maintenance 应成功"

        # maintenance_state 已关闭
        enabled = await maintenance_mode.is_enabled()
        assert enabled is False, \
            "recover_maintenance 成功后 maintenance 应关闭"

        # recover_status 重置为 completed
        recover_status = await _get_recover_status(real_store)
        assert recover_status == "completed", \
            f"recover_maintenance 成功后 recover_status 应为 'completed',实际: {recover_status}"


# ════════════════════════════════════════════════════════════════
# P1-7 测试:Prometheus 指标完善
# ════════════════════════════════════════════════════════════════

def _patch_pe_basics(monkeypatch, kv_mock=None):
    """统一 patch prometheus_exporter 的 SQLite 依赖,避免真实数据库。

    Args:
        monkeypatch: pytest monkeypatch fixture
        kv_mock: _read_kv_value 的 mock 函数(None 时使用默认返回 default)

    Returns:
        prometheus_exporter 模块(已 patch)
    """
    from services import prometheus_exporter as pe

    if kv_mock is None:
        kv_mock = lambda key, default="0": default
    monkeypatch.setattr(pe, "_read_kv_value", kv_mock)
    monkeypatch.setattr(pe, "_read_sqlite_single", lambda db, query, default=0: 0)
    monkeypatch.setattr(pe, "_get_relay_spool_disk_usage", lambda: 0)
    monkeypatch.setattr(pe, "_start_r40_collector", lambda: None)
    monkeypatch.setattr(
        pe, "check_readiness",
        lambda: {"ready": False, "passed": 0, "checks": {}, "details": {},
                 "ru_daily_usage": "unknown", "last_crdb_sync_age": -1,
                 "last_r2_collect_age": -1},
    )
    return pe


class TestHighCardinalityLabelHandling:
    """R51 P1-7: 高基数 label 处理 — CI 模式 raise,运行时丢弃。"""

    def test_high_cardinality_label_ci_mode_raises_app_error(self, monkeypatch):
        """CI 模式检测到高基数 label → raise AppError(METRICS_HIGH_CARDINALITY_LABEL)。

        设置 PROMETHEUS_HIGH_CARDINALITY_MODE=ci 后,
        _check_no_high_cardinality_labels 应 raise AppError 阻断输出。
        """
        from services import prometheus_exporter as pe
        from services.error_codes import AppError, ErrorCodes

        # 强制 CI 模式
        monkeypatch.setattr(pe, "_HIGH_CARDINALITY_MODE", "ci")

        # 构造含高基数 label 的指标行(user_id 是高基数 label)
        bad_line = 'some_metric{user_id="12345"} 1'

        with pytest.raises(AppError) as exc_info:
            pe._check_no_high_cardinality_labels(bad_line)

        # 验证 AppError 的 code 为 METRICS_HIGH_CARDINALITY_LABEL
        assert exc_info.value.code == ErrorCodes.METRICS_HIGH_CARDINALITY_LABEL, \
            f"AppError code 应为 METRICS_HIGH_CARDINALITY_LABEL,实际: {exc_info.value.code}"

    def test_high_cardinality_label_runtime_mode_drops_violating_line(
        self, monkeypatch
    ):
        """运行时模式检测到高基数 label → 返回 False(丢弃该行,不输出)。

        设置 PROMETHEUS_HIGH_CARDINALITY_MODE=runtime 后,
        _check_no_high_cardinality_labels 应返回 False 指示丢弃,
        collect_metrics 输出中不应包含违规指标行。
        """
        from services import prometheus_exporter as pe

        # 强制 runtime 模式
        monkeypatch.setattr(pe, "_HIGH_CARDINALITY_MODE", "runtime")

        # 构造含高基数 label 的指标行(message_id 是高基数 label)
        bad_line = 'some_metric{message_id="67890"} 1'
        result = pe._check_no_high_cardinality_labels(bad_line)
        assert result is False, \
            "运行时模式检测到高基数 label 应返回 False(丢弃该行)"

        # 验证:安全行返回 True
        safe_line = 'some_metric{status="ok"} 1'
        result_safe = pe._check_no_high_cardinality_labels(safe_line)
        assert result_safe is True, \
            "不含高基数 label 的行应返回 True(保留)"

        # 验证:collect_metrics 输出不含高基数 label
        # 通过注入一条违规 metric 到 _format_r40_metrics 输出来测试过滤
        pe_patched = _patch_pe_basics(monkeypatch)
        # 直接调用 collect_metrics 检查输出中无 user_id/chat_id/message_id 等
        output = pe_patched.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("#"):
                continue
            for bad_label in ["user_id", "chat_id", "message_id", "file_code"]:
                assert f'{bad_label}="' not in line, \
                    f"运行时模式不应输出含高基数 label '{bad_label}' 的行: {line}"


class TestPrometheusCollectorFailureHandling:
    """R51 P1-7: 采集器失败时输出 collector_success=0,不输出 0 伪装健康。"""

    def test_pel_collector_failure_outputs_collector_success_zero(self, monkeypatch):
        """PEL 采集失败(无法解析为 float)→ 输出 collector_success=0。

        R52 P1-7: 采集失败时不输出 0 值带 error label(0 可能是真实值,无法区分;
        改为完全不输出主数值,仅输出统一的 tgjiema_collector_success=0)。
        """
        from services import prometheus_exporter as pe

        # Mock kv_store:redis_pel_depth 返回空字符串(无法解析)
        def _mock_kv(key, default="0"):
            if key == "redis_pel_depth":
                return ""  # 空字符串 → 解析失败
            if key == "dlq_depth":
                return "5"  # 正常值
            return default

        pe_patched = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)
        output = pe_patched.collect_metrics()

        # R52 P1-7: 应输出统一 tgjiema_collector_success{collector="redis_pel"} 0
        assert 'tgjiema_collector_success{collector="redis_pel"} 0' in output, \
            "PEL 采集失败时应输出 tgjiema_collector_success{collector=\"redis_pel\"} 0"

    def test_dlq_depth_collector_failure_outputs_collector_success_zero(
        self, monkeypatch
    ):
        """dlq_depth 采集失败(无法解析为 float)→ 输出 collector_success=0。

        R52 P1-7: 采集失败时不输出 0 值带 error label,仅输出统一 collector_success=0。
        """
        from services import prometheus_exporter as pe

        # Mock kv_store:dlq_depth 返回非数字字符串(解析失败)
        def _mock_kv(key, default="0"):
            if key == "dlq_depth":
                return "not_a_number"  # 非数字 → 解析失败
            if key == "redis_pel_depth":
                return "3"  # 正常值
            return default

        pe_patched = _patch_pe_basics(monkeypatch, kv_mock=_mock_kv)
        output = pe_patched.collect_metrics()

        # R52 P1-7: 应输出统一 tgjiema_collector_success{collector="dlq"} 0
        assert 'tgjiema_collector_success{collector="dlq"} 0' in output, \
            "dlq_depth 采集失败时应输出 tgjiema_collector_success{collector=\"dlq\"} 0"

    def test_i18n_missing_key_collector_failure_outputs_collector_success_zero(
        self, monkeypatch
    ):
        """i18n missing-key 采集失败 → 输出 collector_success=0。

        R52 P1-7: 采集失败时不输出 0 值带 error label,仅输出统一 collector_success=0。
        """
        from services import prometheus_exporter as pe

        pe_patched = _patch_pe_basics(monkeypatch)

        # Mock services.i18n.get_i18n_manager 抛异常(采集失败)
        mock_i18n = MagicMock()
        mock_i18n.get_i18n_manager = MagicMock(
            side_effect=RuntimeError("i18n 模块未初始化")
        )
        monkeypatch.setitem(sys.modules, "services.i18n", mock_i18n)

        output = pe_patched.collect_metrics()

        # R52 P1-7: 应输出统一 tgjiema_collector_success{collector="i18n_missing_key"} 0
        assert 'tgjiema_collector_success{collector="i18n_missing_key"} 0' in output, \
            "i18n 采集失败时应输出 tgjiema_collector_success{collector=\"i18n_missing_key\"} 0"


# ════════════════════════════════════════════════════════════════
# P1-7 测试:ru_cost_center 估算 vs 官方标记
# ════════════════════════════════════════════════════════════════

class _FakeCacheStoreForRU:
    """模拟 cache_store:用于 ru_cost_center 测试(支持 get_kv/set_kv)。"""

    def __init__(self):
        self._kv: dict[str, str] = {}
        # _db 为非 None 表示 SQLite 已初始化
        self._db = MagicMock(name="fake_aiosqlite_connection")

    async def get_kv(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set_kv(self, key: str, value: str):
        self._kv[key] = value


def _install_fake_cache_store_for_ru(monkeypatch, fake_store: _FakeCacheStoreForRU):
    """注入 fake cache_store 到 services.ru_cost_center 模块。

    注意:ru_cost_center 在模块导入时通过 ``from database.cache_store import get_cache_store``
    绑定了 get_cache_store 引用,因此必须 patch ``services.ru_cost_center.get_cache_store``
    而非 ``sys.modules["database.cache_store"]``(后者不会影响已绑定的引用)。
    """
    import services.ru_cost_center as _ru_module
    monkeypatch.setattr(_ru_module, "get_cache_store", lambda: fake_store)
    return fake_store


class TestRUCostCenterEstimatedFlag:
    """R51 P1-7: ru_cost_center 估算值标记 ru_estimated=1,官方值标记 ru_estimated=0。"""

    @pytest.mark.asyncio
    async def test_record_usage_default_marks_ru_estimated_1(self, monkeypatch):
        """record_usage 默认标记 ru_estimated=1(估算值)。

        R51 P1-7:record_usage 默认 ru_estimated=True,
        存储到 kv_store 的 JSON 中应包含 "ru_estimated": 1。
        """
        from services import ru_cost_center

        fake_store = _FakeCacheStoreForRU()
        _install_fake_cache_store_for_ru(monkeypatch, fake_store)

        # 调用 record_usage(默认 ru_estimated=True)
        result = await ru_cost_center.record_usage(
            service="up_bot",
            operation="read",
            ru_amount=10,
        )
        assert result is True, "record_usage 应成功"

        # 验证 kv_store 中的 JSON 包含 ru_estimated=1
        today = _get_today_str()
        key = f"ru_usage:{today}:up_bot"
        raw = fake_store._kv.get(key)
        assert raw is not None, "kv_store 应存储 RU 记录"
        data = json.loads(raw)
        assert data.get("ru_estimated") == 1, \
            f"默认 record_usage 应标记 ru_estimated=1(估算值),实际: {data.get('ru_estimated')}"

        # 验证事件明细也包含 ru_estimated 标记
        events = data.get("events", [])
        assert len(events) == 1, "应有一条事件记录"
        assert events[0].get("ru_estimated") == 1, \
            "事件明细应标记 ru_estimated=1"

    @pytest.mark.asyncio
    async def test_record_official_usage_marks_ru_estimated_0(self, monkeypatch):
        """record_official_usage 标记 ru_estimated=0(官方 CockroachDB Cloud Metrics)。

        R51 P1-7:record_official_usage 应将 ru_estimated 设为 0,
        表示数据来自 CockroachDB Cloud 官方 API(非估算)。
        """
        from services import ru_cost_center

        fake_store = _FakeCacheStoreForRU()
        _install_fake_cache_store_for_ru(monkeypatch, fake_store)

        # 调用 record_official_usage(标记 ru_estimated=0)
        result = await ru_cost_center.record_official_usage(
            service="up_bot",
            ru_amount=500,
        )
        assert result is True, "record_official_usage 应成功"

        # 验证 kv_store 中的 JSON 包含 ru_estimated=0
        today = _get_today_str()
        key = f"ru_usage:{today}:up_bot"
        raw = fake_store._kv.get(key)
        assert raw is not None, "kv_store 应存储 RU 记录"
        data = json.loads(raw)
        assert data.get("ru_estimated") == 0, \
            f"record_official_usage 应标记 ru_estimated=0(官方值),实际: {data.get('ru_estimated')}"

        # 验证事件明细也包含 ru_estimated=0
        events = data.get("events", [])
        assert len(events) == 1, "应有一条事件记录"
        assert events[0].get("ru_estimated") == 0, \
            "事件明细应标记 ru_estimated=0(官方值)"

    @pytest.mark.asyncio
    async def test_get_daily_report_includes_by_service_estimated(self, monkeypatch):
        """get_daily_report 返回 by_service_estimated 字段(标记每个服务的估算状态)。

        R51 P1-7:get_daily_report 应返回 by_service_estimated 字段,
        包含每个服务的 ru_estimated 标记(1=估算, 0=官方)。
        """
        from services import ru_cost_center

        fake_store = _FakeCacheStoreForRU()
        _install_fake_cache_store_for_ru(monkeypatch, fake_store)

        # 先记录一些数据:up_bot 估算值,idx_bot 官方值
        await ru_cost_center.record_usage("up_bot", "read", 10)
        await ru_cost_center.record_official_usage("idx_bot", 200)

        # 获取日报
        report = await ru_cost_center.get_daily_report()

        # 验证返回包含 by_service_estimated 字段
        assert "by_service_estimated" in report, \
            "get_daily_report 应返回 by_service_estimated 字段(R51 P1-7)"
        by_estimated = report["by_service_estimated"]
        assert by_estimated.get("up_bot") == 1, \
            f"up_bot 应标记为 ru_estimated=1(估算),实际: {by_estimated.get('up_bot')}"
        assert by_estimated.get("idx_bot") == 0, \
            f"idx_bot 应标记为 ru_estimated=0(官方),实际: {by_estimated.get('idx_bot')}"


class TestPrometheusRuDailyUsageIncludesEstimatedLabel:
    """R51 P1-7: Prometheus tgjiema_ru_daily_usage 指标包含 ru_estimated label。"""

    def test_prometheus_output_includes_ru_estimated_label(self, monkeypatch):
        """collect_metrics 输出的 tgjiema_ru_daily_usage 应包含 ru_estimated label。

        R51 P1-7:tgjiema_ru_daily_usage 指标应包含 ru_estimated label,
        区分估算值(1)与官方 CockroachDB Cloud Metrics(0)。
        """
        from services import prometheus_exporter as pe

        pe_patched = _patch_pe_basics(monkeypatch)

        # 直接注入 _r40_state(模拟已采集的 RU 数据)
        with patch.object(pe, "_r40_state", {
            "maintenance_enabled": 0,
            "ru_daily_usage": {"up_bot": 100, "idx_bot": 200},
            # R51 P1-7:up_bot 估算,idx_bot 官方
            "ru_daily_usage_estimated": {"up_bot": 1, "idx_bot": 0},
            "replica_missing_count": 0,
            "quota_reservations_active": 0,
            "content_reports_pending": 0,
            "approvals_pending": 0,
            "tasks_running": 0,
            "notifications_unread": 0,
            "dlq_depth": 0,
            "outbox_unprocessed": 0,
            "audit_log_events_total": {},
            "ru_operations_total": {},
            "approval_execution_success_rate": 0.0,
            "approval_execution_total": 0,
            "approval_execution_success": 0,
            "notification_delivery_latency_samples": [],
            "repair_success_rate": 0.0,
            "repair_total": 0,
            "repair_success": 0,
            "real_rpo_seconds": -1.0,
            "real_rto_seconds": -1.0,
        }):
            output = pe_patched.collect_metrics()

        # 验证:tgjiema_ru_daily_usage 行包含 ru_estimated label
        lines = output.split("\n")
        ru_usage_lines = [
            line for line in lines
            if line.startswith("tgjiema_ru_daily_usage{") and "ru_estimated" in line
        ]
        assert len(ru_usage_lines) >= 2, \
            f"应至少有 2 条 tgjiema_ru_daily_usage 行(up_bot + idx_bot),实际: {len(ru_usage_lines)}"

        # 验证:up_bot 行 ru_estimated="1"(估算)
        up_bot_line = [
            line for line in ru_usage_lines if 'service="up_bot"' in line
        ]
        assert len(up_bot_line) == 1, "应有 1 条 up_bot 的 tgjiema_ru_daily_usage 行"
        assert 'ru_estimated="1"' in up_bot_line[0], \
            f"up_bot 应标记 ru_estimated=\"1\"(估算),实际: {up_bot_line[0]}"

        # 验证:idx_bot 行 ru_estimated="0"(官方)
        idx_bot_line = [
            line for line in ru_usage_lines if 'service="idx_bot"' in line
        ]
        assert len(idx_bot_line) == 1, "应有 1 条 idx_bot 的 tgjiema_ru_daily_usage 行"
        assert 'ru_estimated="0"' in idx_bot_line[0], \
            f"idx_bot 应标记 ru_estimated=\"0\"(官方),实际: {idx_bot_line[0]}"


# ════════════════════════════════════════════════════════════════
# 辅助:获取今天的日期字符串(YYYYMMDD)
# ════════════════════════════════════════════════════════════════

def _get_today_str() -> str:
    """获取今天的日期字符串(YYYYMMDD)。"""
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y%m%d")
