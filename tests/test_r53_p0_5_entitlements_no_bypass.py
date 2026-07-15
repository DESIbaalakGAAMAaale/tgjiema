"""R53 P0-5: Entitlements 移除生产绕过路径测试。

被测目标:
    - ``services/entitlements.py`` 不再保留生产绕过路径
        * ``set_user_plan`` 已重命名为私有 ``_set_user_plan_internal``
        * production 环境下 ``via_command_bus=False`` → AppError(ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN)
        * production 环境下 ``expected_version=None`` → AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED)
        * development / test 环境仍允许直接调用内部函数(向后兼容)
        * ``set_user_plan_via_command_bus`` 成功路径:验证 approval → CAS → 更新套餐 → 回写 executed
        * ``set_user_plan_via_command_bus`` approval 无效 → 抛 AppError + 回写 failed
        * 所有调用方已迁移(grep 验证无残留 ``set_user_plan(via_command_bus=False)``)

测试策略:
    - 使用真实 SQLite 临时数据库隔离生产数据
    - 通过 ``monkeypatch`` 设置 ``settings.ENVIRONMENT`` 模拟 production 环境
    - 通过 ``unittest.mock.patch`` mock CommandBus 的 claim/mark 函数
    - AppError + ErrorCodes 协议化错误校验
    - 中文注释和日志,英文 raise 消息
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


# ── 辅助:构造真实 Plan 字典(避免 conftest MagicMock settings 干扰) ──
def _make_real_plans():
    """构造包含真实 int 值的 _PLANS 字典。"""
    from services.entitlements import Plan
    return {
        "free": Plan(
            name="free", daily_quota=10, external_daily_quota=0,
            max_file_size=50 * 1024 * 1024, max_concurrent=1,
            retention_days=7, priority_queue="normal", max_collection_items=10,
        ),
        "basic": Plan(
            name="basic", daily_quota=100, external_daily_quota=10,
            max_file_size=500 * 1024 * 1024, max_concurrent=3,
            retention_days=30, priority_queue="normal", max_collection_items=50,
        ),
        "premium": Plan(
            name="premium", daily_quota=1000, external_daily_quota=100,
            max_file_size=2 * 1024 * 1024 * 1024, max_concurrent=10,
            retention_days=90, priority_queue="high", max_collection_items=200,
        ),
    }


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r53_p0_5_test_")
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
async def real_store_with_real_plans(real_store, monkeypatch):
    """创建真实 store 并 patch _PLANS 为真实 int 值的字典。"""
    from services import entitlements
    monkeypatch.setattr(entitlements, "_PLANS", _make_real_plans())
    return real_store


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存,避免跨用例污染。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


async def _insert_user(store, user_id: int, level: str = "free", version: int = 0):
    """插入测试用户到 users_local 表。"""
    try:
        await store._db.execute(
            "INSERT OR REPLACE INTO users_local "
            "(user_id, username, membership_level, version) "
            "VALUES (?, ?, ?, ?)",
            (user_id, f"test_user_{user_id}", level, version),
        )
    except Exception:
        # 老库无 version 列,退化为不带 version
        await store._db.execute(
            "INSERT OR REPLACE INTO users_local "
            "(user_id, username, membership_level) "
            "VALUES (?, ?, ?)",
            (user_id, f"test_user_{user_id}", level),
        )
    await store._db.commit()


async def _insert_command_execution(
    store, action_id: str, principal_id: int = 0, status: str = "approved",
    request_hash: str = "",
):
    """插入测试 command_executions 记录(用于审批验证)。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO command_executions
           (action_id, command_type, principal_id, status, owner,
            lease_until, request_hash, result, created_at, updated_at)
           VALUES (?, 'entitlements_set_plan', ?, ?, NULL, NULL, ?, '', ?, ?)""",
        (action_id, principal_id, status, request_hash, now, now),
    )
    await store._db.commit()


# ════════════════════════════════════════════════════════════════
# 1. 公共 API 形态校验
# ════════════════════════════════════════════════════════════════

class TestR53P0_5_PublicApiShape:
    """R53 P0-5: 公共 API 形态校验 — set_user_plan 已私有化,CommandBus 入口为公共 API。"""

    def test_set_user_plan_is_private(self):
        """``set_user_plan`` 应已重命名为 ``_set_user_plan_internal``(私有)。"""
        from services import entitlements
        assert not hasattr(entitlements, "set_user_plan"), \
            "entitlements.set_user_plan 不应再存在(R53 P0-5 已私有化为 _set_user_plan_internal)"
        assert hasattr(entitlements, "_set_user_plan_internal"), \
            "entitlements._set_user_plan_internal 应存在(R53 P0-5 私有化底层函数)"
        assert hasattr(entitlements, "set_user_plan_via_command_bus"), \
            "entitlements.set_user_plan_via_command_bus 应存在(唯一公共生产入口)"

    def test_internal_function_has_via_command_bus_param(self):
        """``_set_user_plan_internal`` 应保留 ``via_command_bus`` 参数(用于守卫)。"""
        sig = inspect.signature(
            __import__("services.entitlements", fromlist=["_set_user_plan_internal"])._set_user_plan_internal
        )
        assert "via_command_bus" in sig.parameters, \
            "_set_user_plan_internal 应保留 via_command_bus 参数(R53 P0-5 守卫)"
        assert "expected_version" in sig.parameters, \
            "_set_user_plan_internal 应保留 expected_version 参数(CAS)"

    def test_error_codes_registered(self):
        """新增的两个错误码应已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        assert ErrorRegistry.is_registered(
            ErrorCodes.ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN
        ), "ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN 应已注册"
        assert ErrorRegistry.is_registered(
            ErrorCodes.ENTITLEMENTS_EXPECTED_VERSION_REQUIRED
        ), "ENTITLEMENTS_EXPECTED_VERSION_REQUIRED 应已注册"

    def test_error_codes_attributes(self):
        """新增错误码应有正确的 http_status / severity / retryable。"""
        from services.error_codes import ErrorCodes, ErrorRegistry
        # DIRECT_MUTATION_FORBIDDEN: 403, critical, 不可重试
        d = ErrorRegistry.get(ErrorCodes.ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN)
        assert d.http_status == 403, f"DIRECT_MUTATION_FORBIDDEN 应为 403,实际: {d.http_status}"
        assert d.severity == "critical", f"DIRECT_MUTATION_FORBIDDEN 应为 critical,实际: {d.severity}"
        assert d.retryable is False, f"DIRECT_MUTATION_FORBIDDEN 应不可重试,实际: {d.retryable}"
        # EXPECTED_VERSION_REQUIRED: 400, critical, 不可重试
        e = ErrorRegistry.get(ErrorCodes.ENTITLEMENTS_EXPECTED_VERSION_REQUIRED)
        assert e.http_status == 400, f"EXPECTED_VERSION_REQUIRED 应为 400,实际: {e.http_status}"
        assert e.severity == "critical", f"EXPECTED_VERSION_REQUIRED 应为 critical,实际: {e.severity}"
        assert e.retryable is False, f"EXPECTED_VERSION_REQUIRED 应不可重试,实际: {e.retryable}"


# ════════════════════════════════════════════════════════════════
# 2. production 守卫 — 禁止直接修改套餐
# ════════════════════════════════════════════════════════════════

class TestR53P0_5_ProductionGuard:
    """R53 P0-5: production 环境下 _set_user_plan_internal 守卫。"""

    @pytest.mark.asyncio
    async def test_production_via_command_bus_false_raises(
        self, real_store_with_real_plans, monkeypatch,
    ):
        """production 环境 + via_command_bus=False → AppError(ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN)。

        场景:
            1. mock settings.ENVIRONMENT = "production"
            2. _set_user_plan_internal(via_command_bus=False)(默认值)
        预期:
            - 抛 AppError(ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN)
            - 不应触达 DB 操作
        """
        from services import entitlements
        from services.error_codes import AppError, ErrorCodes

        # mock production 环境
        monkeypatch.setattr(entitlements.settings, "ENVIRONMENT", "production")
        assert entitlements._get_environment() == "production", \
            "测试前置:_get_environment() 应返回 production"

        await _insert_user(real_store_with_real_plans, user_id=30001, level="free")

        with pytest.raises(AppError) as exc_info:
            await entitlements._set_user_plan_internal(
                user_id=30001,
                plan_name="basic",
                admin_id=999,
                # via_command_bus 默认 False
            )
        assert exc_info.value.code == ErrorCodes.ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN, \
            f"应抛 DIRECT_MUTATION_FORBIDDEN,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_production_expected_version_none_raises(
        self, real_store_with_real_plans, monkeypatch,
    ):
        """production 环境 + via_command_bus=True + expected_version=None → AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED)。

        场景:
            1. mock settings.ENVIRONMENT = "production"
            2. _set_user_plan_internal(via_command_bus=True, expected_version=None)
        预期:
            - 抛 AppError(ENTITLEMENTS_EXPECTED_VERSION_REQUIRED)
            - 即使 via_command_bus=True,expected_version=None 仍拒绝
        """
        from services import entitlements
        from services.error_codes import AppError, ErrorCodes

        monkeypatch.setattr(entitlements.settings, "ENVIRONMENT", "production")

        await _insert_user(real_store_with_real_plans, user_id=30002, level="free")

        with pytest.raises(AppError) as exc_info:
            await entitlements._set_user_plan_internal(
                user_id=30002,
                plan_name="basic",
                admin_id=999,
                via_command_bus=True,
                expected_version=None,  # 显式 None
            )
        assert exc_info.value.code == ErrorCodes.ENTITLEMENTS_EXPECTED_VERSION_REQUIRED, \
            f"应抛 EXPECTED_VERSION_REQUIRED,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_production_via_command_bus_true_with_expected_version_passes(
        self, real_store_with_real_plans, monkeypatch,
    ):
        """production 环境 + via_command_bus=True + expected_version=1 → 成功。

        场景:
            1. mock settings.ENVIRONMENT = "production"
            2. 用户 version=1
            3. _set_user_plan_internal(via_command_bus=True, expected_version=1)
        预期:
            - 不抛异常,返回 True
            - users_local.membership_level 已更新
        """
        from services import entitlements

        monkeypatch.setattr(entitlements.settings, "ENVIRONMENT", "production")

        await _insert_user(real_store_with_real_plans, user_id=30003, level="free", version=1)

        result = await entitlements._set_user_plan_internal(
            user_id=30003,
            plan_name="basic",
            admin_id=999,
            via_command_bus=True,
            expected_version=1,
        )
        assert result is True, "production + via_command_bus=True + expected_version 应成功"

        # 验证 membership_level 已更新
        cursor = await real_store_with_real_plans._db.execute(
            "SELECT membership_level FROM users_local WHERE user_id = ?",
            (30003,),
        )
        row = await cursor.fetchone()
        assert row[0] == "basic", f"membership_level 应为 basic,实际: {row[0]}"


# ════════════════════════════════════════════════════════════════
# 3. development / test 环境 — 仍可调用内部函数
# ════════════════════════════════════════════════════════════════

class TestR53P0_5_DevelopmentBackwardCompat:
    """R53 P0-5: development / test 环境仍允许直接调用 _set_user_plan_internal。"""

    @pytest.mark.asyncio
    async def test_development_via_command_bus_false_passes(
        self, real_store_with_real_plans, monkeypatch,
    ):
        """development 环境 + via_command_bus=False → 允许直接调用(向后兼容)。"""
        from services import entitlements

        # 默认环境为 development(conftest mock settings 未设置 ENVIRONMENT 或非 production)
        monkeypatch.setattr(entitlements.settings, "ENVIRONMENT", "development")
        assert entitlements._get_environment() == "development"

        await _insert_user(real_store_with_real_plans, user_id=31001, level="free")

        result = await entitlements._set_user_plan_internal(
            user_id=31001,
            plan_name="basic",
            admin_id=999,
            # via_command_bus 默认 False
            # expected_version 默认 None
        )
        assert result is True, "development 环境应允许 via_command_bus=False 直接调用"

    @pytest.mark.asyncio
    async def test_test_environment_via_command_bus_false_passes(
        self, real_store_with_real_plans, monkeypatch,
    ):
        """test 环境 + via_command_bus=False → 允许直接调用。"""
        from services import entitlements

        monkeypatch.setattr(entitlements.settings, "ENVIRONMENT", "test")
        assert entitlements._get_environment() == "test"

        await _insert_user(real_store_with_real_plans, user_id=31002, level="free")

        result = await entitlements._set_user_plan_internal(
            user_id=31002,
            plan_name="premium",
            admin_id=999,
        )
        assert result is True, "test 环境应允许 via_command_bus=False 直接调用"


# ════════════════════════════════════════════════════════════════
# 4. set_user_plan_via_command_bus 成功路径
# ════════════════════════════════════════════════════════════════

class TestR53P0_5_CommandBusSuccessPath:
    """R53 P0-5: set_user_plan_via_command_bus 成功路径覆盖。"""

    @pytest.mark.asyncio
    async def test_success_path_approves_cas_updates_and_marks_executed(
        self, real_store_with_real_plans,
    ):
        """成功路径:验证 approval → CAS → 更新套餐 → 回写 executed。

        场景:
            1. 插入 approved 状态的 command_executions 记录
            2. mock claim_execution_approved 返回 True
            3. 调用 set_user_plan_via_command_bus(expected_version=1)
        预期:
            - 返回 {"success": True}
            - users_local.membership_level 更新为 basic
            - command_executions.status 更新为 executed
            - mark_approved_executed 被调用
        """
        from services import entitlements

        action_id = "r53_p0_5_success_001"
        await _insert_command_execution(
            real_store_with_real_plans,
            action_id=action_id,
            principal_id=950,
            status="approved",
            request_hash="hash_001",
        )
        await _insert_user(
            real_store_with_real_plans, user_id=32001, level="free", version=1,
        )

        principal = MagicMock()
        principal.id = 950
        principal.name = "admin_950"
        principal.source = "web"

        # 真实 claim_execution_approved 会做 CAS UPDATE approved→executing
        # mark_approved_executed 会做 UPDATE executing→executed
        result = await entitlements.set_user_plan_via_command_bus(
            user_id=32001,
            plan_name="basic",
            principal=principal,
            action_id=action_id,
            request_hash="hash_001",
            expected_version=1,
        )
        assert result == {"success": True}, f"应返回 success,实际: {result}"

        # 验证 membership_level 已更新
        cursor = await real_store_with_real_plans._db.execute(
            "SELECT membership_level FROM users_local WHERE user_id = ?",
            (32001,),
        )
        row = await cursor.fetchone()
        assert row[0] == "basic", f"membership_level 应为 basic,实际: {row[0]}"

        # 验证 command_executions.status 应为 'executed'(由 mark_approved_executed 写入)
        cursor = await real_store_with_real_plans._db.execute(
            "SELECT status FROM command_executions WHERE action_id = ?",
            (action_id,),
        )
        ce_row = await cursor.fetchone()
        assert ce_row[0] == "executed", \
            f"command_executions.status 应为 executed,实际: {ce_row[0]}"

    @pytest.mark.asyncio
    async def test_success_path_writes_audit_log_with_via_command_bus(
        self, real_store_with_real_plans,
    ):
        """成功路径应写入 audit_log,且 details.via_command_bus=True。"""
        from services import entitlements

        action_id = "r53_p0_5_audit_001"
        await _insert_command_execution(
            real_store_with_real_plans,
            action_id=action_id,
            principal_id=951,
            status="approved",
            request_hash="hash_audit",
        )
        await _insert_user(
            real_store_with_real_plans, user_id=32002, level="free", version=1,
        )

        principal = MagicMock()
        principal.id = 951
        principal.name = "admin_951"

        await entitlements.set_user_plan_via_command_bus(
            user_id=32002,
            plan_name="premium",
            principal=principal,
            action_id=action_id,
            request_hash="hash_audit",
            expected_version=1,
        )

        # 查询 audit_log
        rows = await real_store_with_real_plans._db.execute_fetchall(
            "SELECT details FROM audit_log WHERE action = 'set_plan' "
            "AND target_id = ? ORDER BY id DESC LIMIT 1",
            ("32002",),
        )
        assert rows and rows[0], "应找到 audit_log 记录"
        details = json.loads(rows[0][0])
        assert details.get("via_command_bus") is True, \
            f"audit_log.via_command_bus 应为 True,实际: {details.get('via_command_bus')}"
        assert details.get("new_plan") == "premium"
        assert details.get("old_plan") == "free"


# ════════════════════════════════════════════════════════════════
# 5. set_user_plan_via_command_bus approval 无效
# ════════════════════════════════════════════════════════════════

class TestR53P0_5_CommandBusApprovalInvalid:
    """R53 P0-5: set_user_plan_via_command_bus approval 无效时回写 failed。"""

    @pytest.mark.asyncio
    async def test_invalid_approval_raises_and_marks_failed(
        self, real_store_with_real_plans,
    ):
        """approval 无效(claim 返回 False)→ 抛 AppError + 回写 failed。

        场景:
            1. mock claim_execution_approved 返回 False(状态非 approved 或已被抢占)
            2. 调用 set_user_plan_via_command_bus
        预期:
            - 抛 AppError(ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS)
            - mark_approved_failed 被调用(回写 failed)
            - users_local 未被修改
        """
        from services import entitlements
        from services.error_codes import AppError, ErrorCodes

        await _insert_user(
            real_store_with_real_plans, user_id=33001, level="free", version=1,
        )

        principal = MagicMock()
        principal.id = 960
        principal.name = "admin_960"

        # mock CommandBus 模块内函数
        with patch("services.command_bus.claim_execution_approved", new=AsyncMock(return_value=False)), \
             patch("services.command_bus.mark_approved_failed", new=AsyncMock(return_value=True)) as mock_failed, \
             patch("services.command_bus.mark_approved_executed", new=AsyncMock(return_value=True)) as mock_executed:
            with pytest.raises(AppError) as exc_info:
                await entitlements.set_user_plan_via_command_bus(
                    user_id=33001,
                    plan_name="basic",
                    principal=principal,
                    action_id="r53_p0_5_invalid_001",
                    request_hash="hash_invalid",
                    expected_version=1,
                )
            assert exc_info.value.code == ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS, \
                f"应抛 REQUIRES_COMMAND_BUS,实际: {exc_info.value.code}"

            # mark_approved_failed 应被调用(回写 failed)
            assert mock_failed.await_count == 1, \
                "approval 无效时 mark_approved_failed 应被调用一次"
            # mark_approved_executed 不应被调用
            assert mock_executed.await_count == 0, \
                "approval 无效时 mark_approved_executed 不应被调用"

        # users_local 未被修改(membership_level 仍为 free)
        cursor = await real_store_with_real_plans._db.execute(
            "SELECT membership_level FROM users_local WHERE user_id = ?",
            (33001,),
        )
        row = await cursor.fetchone()
        assert row[0] == "free", \
            f"approval 无效时 users_local 不应被修改,实际 membership_level: {row[0]}"

    @pytest.mark.asyncio
    async def test_empty_action_id_raises_without_claim(
        self, real_store_with_real_plans,
    ):
        """action_id 为空 → 抛 AppError,不调用 claim_execution_approved。"""
        from services import entitlements
        from services.error_codes import AppError, ErrorCodes

        principal = MagicMock()
        principal.id = 961
        principal.name = "admin_961"

        with patch("services.command_bus.claim_execution_approved", new=AsyncMock(return_value=True)) as mock_claim:
            with pytest.raises(AppError) as exc_info:
                await entitlements.set_user_plan_via_command_bus(
                    user_id=33002,
                    plan_name="basic",
                    principal=principal,
                    action_id="",  # 空 action_id
                )
            assert exc_info.value.code == ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS

            # action_id 为空时不应调用 claim_execution_approved
            assert mock_claim.await_count == 0, \
                "action_id 为空时不应调用 claim_execution_approved"


# ════════════════════════════════════════════════════════════════
# 6. 调用方迁移完整性校验(grep 验证无残留)
# ════════════════════════════════════════════════════════════════

class TestR53P0_5_NoResidualDirectBypass:
    """R53 P0-5: grep 验证源码无残留 ``set_user_plan(via_command_bus=False)`` 调用。"""

    def test_no_residual_set_user_plan_public_function(self):
        """``services/entitlements.py`` 中不应再存在 ``async def set_user_plan(``(公共函数定义)。"""
        src = (Path(__file__).resolve().parent.parent / "services" / "entitlements.py").read_text(
            encoding="utf-8"
        )
        # 严格匹配 async def set_user_plan(— 不应再作为公共函数定义
        assert "async def set_user_plan(" not in src, \
            "services/entitlements.py 不应再定义公共 set_user_plan(R53 P0-5 已私有化)"

    def test_no_residual_via_command_bus_false_calls_in_services(self):
        """``services/`` 目录下不应有 ``set_user_plan(via_command_bus=False)`` 直接调用残留。"""
        services_dir = Path(__file__).resolve().parent.parent / "services"
        for py_file in services_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            # 排除注释行(粗略过滤)
            for idx, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                # 严格匹配 set_user_plan(... via_command_bus=False ...)
                if "set_user_plan(" in line and "via_command_bus=False" in line:
                    # 允许在 services/entitlements.py 内部定义或调用 _set_user_plan_internal
                    # (定义中有 via_command_bus: bool = False 是参数声明,不是调用)
                    if py_file.name == "entitlements.py":
                        # 检查是否为参数声明("via_command_bus: bool = False" 形式)
                        if "via_command_bus: bool = False" in line:
                            continue
                        # 检查是否为注释或文档字符串中(粗略判断)
                        if "#" in line and line.index("#") < line.index("set_user_plan"):
                            continue
                    pytest.fail(
                        f"发现残留直接绕过调用: {py_file}:{idx}: {line.strip()}"
                    )

    def test_no_residual_set_user_plan_calls_in_bots_admin(self):
        """``bots/`` 和 ``admin/`` 目录下不应有 ``set_user_plan(`` 直接调用残留。"""
        repo_root = Path(__file__).resolve().parent.parent
        for sub_dir in ("bots", "admin"):
            d = repo_root / sub_dir
            if not d.exists():
                continue
            for py_file in d.rglob("*.py"):
                if "__pycache__" in str(py_file):
                    continue
                try:
                    content = py_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                for idx, line in enumerate(content.splitlines(), 1):
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    # 检查是否调用 set_user_plan(而不是 _set_user_plan_internal 或 set_user_plan_via_command_bus)
                    if "set_user_plan(" in line:
                        # 排除 set_user_plan_via_command_bus(允许的公共 API)
                        if "set_user_plan_via_command_bus(" in line:
                            continue
                        # 排除 _set_user_plan_internal(允许的私有 API,但 bots/admin 不应直接调用)
                        if "_set_user_plan_internal(" in line:
                            continue
                        # 排除注释中的字符串引用
                        if "#" in line and line.index("#") < line.index("set_user_plan"):
                            continue
                        pytest.fail(
                            f"{sub_dir}/ 中发现残留 set_user_plan 调用: "
                            f"{py_file}:{idx}: {line.strip()}"
                        )

    def test_no_residual_public_set_user_plan_import(self):
        """``tests/`` 目录下不应再有 ``from services.entitlements import set_user_plan`` 残留。

        注意:允许导入 ``_set_user_plan_internal`` 或 ``set_user_plan_via_command_bus``。
        """
        tests_dir = Path(__file__).resolve().parent
        for py_file in tests_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            if py_file.name == Path(__file__).name:
                continue  # 跳过本测试文件
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for idx, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                # 检查 ``from services.entitlements import ... set_user_plan ...``
                if "from services.entitlements import" in line and "set_user_plan" in line:
                    # 排除 _set_user_plan_internal 和 set_user_plan_via_command_bus
                    if "_set_user_plan_internal" in line:
                        continue
                    if "set_user_plan_via_command_bus" in line:
                        continue
                    # 如果还有纯 ``set_user_plan``(无前缀),则为残留
                    # 简单检查:行中是否有未带前缀的 set_user_plan
                    import re
                    # 匹配 set_user_plan 但前面不是 _ 也不是 via_command_bus
                    if re.search(r"(?<![_\w])set_user_plan(?!\w)(?!\s*via_command_bus)", line):
                        pytest.fail(
                            f"tests/ 中发现残留 import set_user_plan: "
                            f"{py_file}:{idx}: {line.strip()}"
                        )
