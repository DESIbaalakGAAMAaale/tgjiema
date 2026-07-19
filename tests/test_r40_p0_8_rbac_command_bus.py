"""R40 P0-8: RBAC + CommandBus 强制门禁测试。

测试覆盖:
- CommandBus.execute() 直接执行(非审批命令)
- CommandBus.execute() 高风险命令需审批
- 权限不足拒绝
- RBAC fail-closed(DB 异常时拒绝)
- 幂等:重复 action_id 只执行一次
- execute_approved_action() 审批后执行
- 状态机:mark_executing/mark_executed/mark_failed
- 命令工厂函数构造正确的 Command 对象
- HIGH_RISK_COMMAND_REGISTRY 注册完整性
- 灾备恢复必须审批

测试策略:
- 使用 CommandBus 构造函数注入 mock rbac/approval 模块(隔离 DB 依赖)
- 使用 AsyncMock 模拟异步函数
- R41 P0-5: 幂等缓存迁移到 SQLite command_executions 表,
  需要数据库初始化的测试通过 ``real_store`` fixture 注入临时 SQLite 数据库
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# 延迟导入,确保 conftest 已注入 fake config
from services.command_bus import (
    AdminPrincipal,
    Command,
    CommandBus,
    HIGH_RISK_COMMAND_REGISTRY,
    PERM_CONTENT_TAKEDOWN,
    PERM_DATA_PURGE,
    PERM_DISASTER_RESTORE,
    PERM_MAINTENANCE_DISABLE,
    PERM_MAINTENANCE_ENABLE,
    PERM_RBAC_ASSIGN,
    PERM_USERS_BAN,
    PERM_USERS_UNBAN,
    Result,
    APPROVAL_ACTION_TAKEDOWN,
    APPROVAL_ACTION_BAN,
    APPROVAL_ACTION_RESTORE,
    APPROVAL_ACTION_RBAC_ASSIGN,
    APPROVAL_ACTION_MAINTENANCE_ENABLE,
    APPROVAL_ACTION_MAINTENANCE_DISABLE,
    APPROVAL_ACTION_DELETE_DATA,
    _generate_action_id,
    reset_idempotency_cache,
    make_takedown_command,
    make_ban_user_command,
    make_unban_user_command,
    make_assign_role_command,
    make_restore_backup_command,
    make_enable_maintenance_command,
    make_disable_maintenance_command,
    make_purge_data_command,
)
from database import cache_store as _cs_module
from database.cache_store import CacheStore


# ════════════════════════════════════════════════════════════════
# 辅助: 构造 mock rbac / approval 模块
# ════════════════════════════════════════════════════════════════

def _make_mock_rbac(has_perm: bool = True):
    """构造 mock rbac 模块,check_permission 返回 has_perm。"""
    mock = MagicMock(name="mock_rbac")
    mock.check_permission = AsyncMock(return_value=has_perm)
    return mock


def _make_mock_approval(create_approval_id: int = 100):
    """构造 mock approval 模块。

    默认 create_approval 返回 100,其他状态函数返回 True。
    """
    mock = MagicMock(name="mock_approval")
    mock.create_approval = AsyncMock(return_value=create_approval_id)
    mock.APPROVAL_STATUS_APPROVED = "approved"
    mock.APPROVAL_STATUS_EXECUTING = "executing"
    mock.APPROVAL_STATUS_EXECUTED = "executed"
    mock.APPROVAL_STATUS_FAILED = "failed"
    mock.get_approval = AsyncMock(return_value={
        "id": create_approval_id,
        "status": "approved",
        "payload": {
            "command_action": "takedown_report",
            "params": {"target_type": "file_code", "target_id": "ABC123", "reason": ""},
            "principal_id": 1,
            "principal_name": "admin",
            "principal_source": "bot",
            "action_id": "test_action_id",
        },
    })
    mock.mark_executing = AsyncMock(return_value=True)
    mock.mark_executed = AsyncMock(return_value=True)
    mock.mark_failed = AsyncMock(return_value=True)
    return mock


@pytest.fixture(autouse=True)
def _reset_idempotency():
    """每个用例前重置幂等缓存,避免跨用例污染。

    R41 P0-5: reset_idempotency_cache 已弃用(幂等改用 SQLite command_executions 表),
    此 fixture 保留为兼容点;真正需要幂等保护的测试应使用 ``real_store`` fixture
    提供临时 SQLite 数据库。
    """
    reset_idempotency_cache()
    yield
    reset_idempotency_cache()


@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    R41 P0-5: 幂等缓存迁移到 SQLite command_executions 表后,
    需要数据库初始化才能进行幂等检查。此 fixture 提供临时 SQLite 数据库,
    并设置 ``_cs_module._store`` 使 ``get_cache_store()`` 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r40_p0_8_test_")
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


# ════════════════════════════════════════════════════════════════
# 1. CommandBus.execute() 测试
# ════════════════════════════════════════════════════════════════

class TestCommandBusExecute:
    """R40 P0-8: CommandBus.execute() 测试。"""

    @pytest.mark.asyncio
    async def test_non_approval_command_success(self):
        """不需审批的命令(如 unban_user)权限通过后直接执行 handler。"""
        handler_calls = []

        async def _handler(params):
            handler_calls.append(params)
            return {"unban_ok": True}

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 12345},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")
        bus = CommandBus(rbac_module=_make_mock_rbac(has_perm=True))

        result = await bus.execute(command, principal)

        assert result.success is True
        assert result.approval_required is False
        assert result.data == {"unban_ok": True}
        assert result.error == ""
        assert result.action_id  # 非空
        assert len(handler_calls) == 1
        assert handler_calls[0] == {"user_id": 12345}

    @pytest.mark.asyncio
    async def test_high_risk_command_requires_approval(self):
        """高风险命令(takedown)权限通过后创建审批,不直接执行 handler。"""
        handler_calls = []

        async def _handler(params):
            handler_calls.append(params)
            return {"takedown_ok": True}

        command = Command(
            action="takedown_report",
            required_permission=PERM_CONTENT_TAKEDOWN,
            handler=_handler,
            params={"target_type": "file_code", "target_id": "ABC", "reason": ""},
            requires_approval=True,
            approval_action=APPROVAL_ACTION_TAKEDOWN,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")
        mock_approval = _make_mock_approval(create_approval_id=42)
        bus = CommandBus(
            rbac_module=_make_mock_rbac(has_perm=True),
            approval_module=mock_approval,
        )

        result = await bus.execute(command, principal)

        # 应创建审批,不执行 handler
        assert result.success is False
        assert result.approval_required is True
        assert result.approval_id == 42
        assert "需要审批" in result.error
        assert len(handler_calls) == 0  # handler 未执行

        # 验证 create_approval 被调用,参数包含 command_action
        mock_approval.create_approval.assert_called_once()
        call_args = mock_approval.create_approval.call_args
        assert call_args.kwargs["action"] == APPROVAL_ACTION_TAKEDOWN
        payload = call_args.kwargs["payload"]
        assert payload["command_action"] == "takedown_report"
        assert payload["principal_id"] == 1
        assert payload["principal_source"] == "bot"

    @pytest.mark.asyncio
    async def test_permission_denied_rejection(self):
        """权限不足时拒绝执行,返回 error 含权限标识。"""
        async def _handler(params):
            return {"ok": True}

        command = Command(
            action="ban_user",
            required_permission=PERM_USERS_BAN,
            handler=_handler,
            params={"user_id": 123},
            requires_approval=True,
            approval_action=APPROVAL_ACTION_BAN,
        )
        principal = AdminPrincipal(id=2, name="support_user", source="bot")
        bus = CommandBus(rbac_module=_make_mock_rbac(has_perm=False))

        result = await bus.execute(command, principal)

        assert result.success is False
        assert result.approval_required is False
        assert "权限不足" in result.error
        assert PERM_USERS_BAN in result.error

    @pytest.mark.asyncio
    async def test_rbac_exception_fail_closed(self):
        """RBAC check_permission 抛异常时 fail-closed 拒绝执行。"""
        async def _handler(params):
            return {"ok": True}

        command = Command(
            action="ban_user",
            required_permission=PERM_USERS_BAN,
            handler=_handler,
            params={"user_id": 123},
        )
        principal = AdminPrincipal(id=3, name="ops_user", source="bot")

        # 构造会抛异常的 mock rbac
        mock_rbac = MagicMock()
        mock_rbac.check_permission = AsyncMock(side_effect=RuntimeError("DB connection lost"))
        bus = CommandBus(rbac_module=mock_rbac)

        result = await bus.execute(command, principal)

        # fail-closed: 异常时拒绝
        assert result.success is False
        assert "权限校验异常" in result.error
        assert "DB connection lost" in result.error

    @pytest.mark.asyncio
    async def test_idempotency_duplicate_action_id(self, real_store):
        """相同 action_id 第二次执行直接返回缓存结果,不重复执行 handler。

        R41 P0-5: 幂等缓存迁移到 SQLite command_executions 表,
        需要通过 ``real_store`` fixture 提供临时 SQLite 数据库。
        """
        call_count = {"n": 0}

        async def _handler(params):
            call_count["n"] += 1
            return {"ok": True}

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 999},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")
        bus = CommandBus(rbac_module=_make_mock_rbac(has_perm=True))

        # 固定 action_id 实现幂等
        fixed_action_id = "idempotent_test_id_001"
        result1 = await bus.execute(command, principal, action_id=fixed_action_id)
        result2 = await bus.execute(command, principal, action_id=fixed_action_id)

        assert result1.success is True
        assert result2.success is True
        assert result2.action_id == fixed_action_id
        # handler 只被调用一次(SQLite command_executions 表幂等命中)
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_create_approval_failure_returns_error(self):
        """create_approval 返回 -1(失败)时,execute 返回错误。"""
        async def _handler(params):
            return {"ok": True}

        command = Command(
            action="takedown_report",
            required_permission=PERM_CONTENT_TAKEDOWN,
            handler=_handler,
            params={"target_type": "file_code", "target_id": "X", "reason": ""},
            requires_approval=True,
            approval_action=APPROVAL_ACTION_TAKEDOWN,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")
        mock_approval = _make_mock_approval(create_approval_id=-1)
        bus = CommandBus(
            rbac_module=_make_mock_rbac(has_perm=True),
            approval_module=mock_approval,
        )

        result = await bus.execute(command, principal)

        assert result.success is False
        assert "无效 approval_id" in result.error

    @pytest.mark.asyncio
    async def test_handler_exception_returns_failure(self):
        """handler 抛异常时返回失败结果。"""
        async def _handler(params):
            raise RuntimeError("handler 业务异常")

        command = Command(
            action="unban_user",
            required_permission=PERM_USERS_UNBAN,
            handler=_handler,
            params={"user_id": 1},
            requires_approval=False,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")
        bus = CommandBus(rbac_module=_make_mock_rbac(has_perm=True))

        result = await bus.execute(command, principal)

        assert result.success is False
        assert "执行失败" in result.error
        assert "handler 业务异常" in result.error


# ════════════════════════════════════════════════════════════════
# 2. CommandBus.execute_approved_action() 测试
# ════════════════════════════════════════════════════════════════

class TestExecuteApprovedAction:
    """R40 P0-8: 审批通过后执行(execute_approved_action)测试。"""

    @pytest.mark.asyncio
    async def test_execute_approved_action_success(self, monkeypatch):
        """审批通过后 execute_approved_action 成功执行 handler。

        使用真实的命令工厂函数 + monkeypatch 替换 handler 中的 content_reports。
        """
        # 使用真实工厂函数(其内部 handler 会 import content_reports)
        # 通过 monkeypatch 替换 content_reports.takedown_content
        import services.content_reports as cr_mod
        monkeypatch.setattr(
            cr_mod, "takedown_content",
            AsyncMock(return_value=True),
        )

        command = make_takedown_command(
            target_type="file_code", target_id="ABC", reason="test",
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")

        # 构造 mock approval,返回 approved 状态的审批记录
        action_id = "approve_test_001"
        mock_approval = _make_mock_approval(create_approval_id=200)
        mock_approval.get_approval = AsyncMock(return_value={
            "id": 200,
            "status": "approved",
            "payload": {
                "command_action": "takedown_report",
                "params": command.params,
                "principal_id": 1,
                "principal_name": "admin",
                "principal_source": "bot",
                "action_id": action_id,
            },
        })

        bus = CommandBus(
            rbac_module=_make_mock_rbac(has_perm=True),
            approval_module=mock_approval,
        )

        result = await bus.execute_approved_action(200, action_id=action_id)

        assert result.success is True
        assert result.data == {"takedown_ok": True}
        # 验证状态转换被调用
        mock_approval.mark_executing.assert_called_once_with(200)
        mock_approval.mark_executed.assert_called_once_with(200)
        mock_approval.mark_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_approved_action_idempotent(self, real_store, monkeypatch):
        """对已执行的 action_id 再次调用,返回缓存结果不重复执行。

        R41 P0-5: 幂等缓存迁移到 SQLite command_executions 表,
        需要通过 ``real_store`` fixture 提供临时 SQLite 数据库。
        """
        import services.content_reports as cr_mod
        call_count = {"n": 0}

        async def _fake_takedown(*args, **kwargs):
            call_count["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        command = make_takedown_command(
            target_type="file_code", target_id="XYZ", reason="",
        )
        action_id = "idempotent_approval_001"
        mock_approval = _make_mock_approval(create_approval_id=300)
        mock_approval.get_approval = AsyncMock(return_value={
            "id": 300,
            "status": "approved",
            "payload": {
                "command_action": "takedown_report",
                "params": command.params,
                "principal_id": 1,
                "principal_name": "admin",
                "principal_source": "bot",
                "action_id": action_id,
            },
        })

        bus = CommandBus(
            rbac_module=_make_mock_rbac(has_perm=True),
            approval_module=mock_approval,
        )

        result1 = await bus.execute_approved_action(300, action_id=action_id)
        result2 = await bus.execute_approved_action(300, action_id=action_id)

        assert result1.success is True
        assert result2.success is True
        # handler 只被调用一次(SQLite command_executions 表幂等命中)
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_execute_approved_action_wrong_status(self):
        """审批状态非 approved 时拒绝执行。"""
        mock_approval = _make_mock_approval(create_approval_id=400)
        mock_approval.get_approval = AsyncMock(return_value={
            "id": 400,
            "status": "pending",  # 非 approved
            "payload": {},
        })
        bus = CommandBus(approval_module=mock_approval)

        result = await bus.execute_approved_action(400)

        assert result.success is False
        assert "非 approved" in result.error

    @pytest.mark.asyncio
    async def test_execute_approved_action_not_found(self):
        """审批记录不存在时返回失败。"""
        mock_approval = _make_mock_approval()
        mock_approval.get_approval = AsyncMock(return_value=None)
        bus = CommandBus(approval_module=mock_approval)

        result = await bus.execute_approved_action(999)

        assert result.success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_execute_approved_action_handler_failure_marks_failed(self, monkeypatch):
        """handler 执行失败时,审批状态标记为 FAILED。"""
        import services.content_reports as cr_mod
        # 让 takedown_content 抛异常
        monkeypatch.setattr(
            cr_mod, "takedown_content",
            AsyncMock(side_effect=RuntimeError("目标不存在")),
        )

        command = make_takedown_command(
            target_type="file_code", target_id="BAD", reason="",
        )
        action_id = "fail_test_001"
        mock_approval = _make_mock_approval(create_approval_id=500)
        mock_approval.get_approval = AsyncMock(return_value={
            "id": 500,
            "status": "approved",
            "payload": {
                "command_action": "takedown_report",
                "params": command.params,
                "principal_id": 1,
                "principal_name": "admin",
                "principal_source": "bot",
                "action_id": action_id,
            },
        })

        bus = CommandBus(
            rbac_module=_make_mock_rbac(has_perm=True),
            approval_module=mock_approval,
        )

        result = await bus.execute_approved_action(500, action_id=action_id)

        assert result.success is False
        assert "执行失败" in result.error
        # 验证 mark_failed 被调用
        mock_approval.mark_executing.assert_called_once_with(500)
        mock_approval.mark_failed.assert_called_once()
        mock_approval.mark_executed.assert_not_called()


# ════════════════════════════════════════════════════════════════
# 3. RBAC fail-closed 测试
# ════════════════════════════════════════════════════════════════

class TestRbacFailClosed:
    """R40 P0-8: RBAC fail-closed 行为测试。"""

    @pytest.mark.asyncio
    async def test_check_permission_db_exception_returns_false(self, monkeypatch):
        """check_permission 在 DB 异常时返回 False(fail-closed)。"""
        from services import rbac

        # 让 get_user_role 抛异常
        async def _fail_get_role(user_id):
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr(rbac, "get_user_role", _fail_get_role)

        result = await rbac.check_permission(user_id=1, permission="users:ban")

        assert result is False  # fail-closed

    @pytest.mark.asyncio
    async def test_check_permission_no_role_returns_false(self, monkeypatch):
        """用户无角色时 check_permission 返回 False。"""
        from services import rbac

        async def _no_role(user_id):
            return None

        monkeypatch.setattr(rbac, "get_user_role", _no_role)

        result = await rbac.check_permission(user_id=999, permission="users:ban")

        assert result is False

    @pytest.mark.asyncio
    async def test_list_user_permissions_db_unavailable_returns_empty(self, monkeypatch):
        """DB 不可用时 list_user_permissions 返回空列表(fail-closed)。"""
        from services import rbac
        from database import cache_store

        # 让 get_user_role 返回有效角色名
        async def _has_role(user_id):
            return "security"

        monkeypatch.setattr(rbac, "get_user_role", _has_role)

        # 让 get_cache_store 返回 _db=None 的 mock(模拟 DB 未初始化)
        mock_store = MagicMock()
        mock_store._db = None
        monkeypatch.setattr(cache_store, "get_cache_store", lambda: mock_store)
        # rbac 在 import 时已绑定 get_cache_store,需要 patch rbac 模块
        monkeypatch.setattr(rbac, "get_cache_store", lambda: mock_store)

        perms = await rbac.list_user_permissions(user_id=1)

        # fail-closed: DB 不可用时返回空列表,不返回 _DEFAULT_ROLE_PERMISSIONS 回退
        assert perms == []

    @pytest.mark.asyncio
    async def test_super_admin_wildcard_permission(self, monkeypatch):
        """super_admin 角色拥有 ["*"] 通配权限,任意 permission 返回 True。"""
        from services import rbac

        async def _super_admin_role(user_id):
            return "super_admin"

        monkeypatch.setattr(rbac, "get_user_role", _super_admin_role)

        async def _wildcard_perms(user_id):
            return ["*"]

        monkeypatch.setattr(rbac, "list_user_permissions", _wildcard_perms)

        # 任意权限都应通过
        assert await rbac.check_permission(1, "users:ban") is True
        assert await rbac.check_permission(1, "content:takedown") is True
        assert await rbac.check_permission(1, "any:permission") is True

    @pytest.mark.asyncio
    async def test_get_principal_permissions_returns_set(self, monkeypatch):
        """get_principal_permissions 返回 set 类型。"""
        from services import rbac

        async def _perms(user_id):
            return ["users:ban", "users:unban", "content:takedown"]

        monkeypatch.setattr(rbac, "list_user_permissions", _perms)

        result = await rbac.get_principal_permissions(1)

        assert isinstance(result, set)
        assert "users:ban" in result
        assert "content:takedown" in result
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_principal_permissions_exception_returns_empty_set(self, monkeypatch):
        """get_principal_permissions 异常时返回空集合(fail-closed)。"""
        from services import rbac

        async def _fail(user_id):
            raise RuntimeError("DB 故障")

        monkeypatch.setattr(rbac, "list_user_permissions", _fail)

        result = await rbac.get_principal_permissions(1)

        assert isinstance(result, set)
        assert len(result) == 0


# ════════════════════════════════════════════════════════════════
# 4. 命令工厂函数测试
# ════════════════════════════════════════════════════════════════

class TestCommandFactories:
    """R40 P0-8: 命令工厂函数构造正确性测试。"""

    def test_make_takedown_command(self):
        """make_takedown_command 构造正确,需审批。"""
        cmd = make_takedown_command(
            target_type="file_code", target_id="ABC123", reason="违规内容",
        )
        assert cmd.action == "takedown_report"
        assert cmd.required_permission == PERM_CONTENT_TAKEDOWN
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_TAKEDOWN
        assert cmd.params["target_type"] == "file_code"
        assert cmd.params["target_id"] == "ABC123"
        assert cmd.params["reason"] == "违规内容"
        assert cmd.handler is not None

    def test_make_ban_user_command(self):
        """make_ban_user_command 构造正确,需审批。"""
        cmd = make_ban_user_command(user_id=12345, reason="违规", duration_days=7)
        assert cmd.action == "ban_user"
        assert cmd.required_permission == PERM_USERS_BAN
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_BAN
        assert cmd.params["user_id"] == 12345
        assert cmd.params["reason"] == "违规"
        assert cmd.params["duration_days"] == 7

    def test_make_unban_user_command_requires_approval(self):
        """R64 P0-05: make_unban_user_command 构造正确,统一走审批门禁。"""
        cmd = make_unban_user_command(user_id=12345)
        assert cmd.action == "unban_user"
        assert cmd.required_permission == PERM_USERS_UNBAN
        # R64 P0-05: unban_user 改为 requires_approval=True(高风险逆操作)
        assert cmd.requires_approval is True
        # 复用 ban 审批 action(逆操作)
        assert cmd.approval_action == APPROVAL_ACTION_BAN
        assert cmd.params["user_id"] == 12345

    def test_make_assign_role_command(self):
        """make_assign_role_command 构造正确,需审批。"""
        cmd = make_assign_role_command(user_id=999, role_name="security")
        assert cmd.action == "assign_role"
        assert cmd.required_permission == PERM_RBAC_ASSIGN
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_RBAC_ASSIGN
        assert cmd.params["user_id"] == 999
        assert cmd.params["role_name"] == "security"

    def test_make_restore_backup_command_requires_approval(self):
        """make_restore_backup_command 构造正确,必须审批。"""
        cmd = make_restore_backup_command(backup_id="backup_20260713_001")
        assert cmd.action == "restore_backup"
        assert cmd.required_permission == PERM_DISASTER_RESTORE
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_RESTORE
        assert cmd.params["backup_id"] == "backup_20260713_001"
        # R40 P0-8: 扩展参数 tables/merge(默认 None/False)
        assert cmd.params["tables"] is None
        assert cmd.params["merge"] is False

    def test_make_restore_backup_command_with_tables_and_merge(self):
        """make_restore_backup_command 支持选择性恢复参数(tables/merge)。"""
        cmd = make_restore_backup_command(
            backup_id="backup_20260713_002",
            tables=["users", "file_records"],
            merge=True,
        )
        assert cmd.params["backup_id"] == "backup_20260713_002"
        assert cmd.params["tables"] == ["users", "file_records"]
        assert cmd.params["merge"] is True

    def test_make_delete_file_command_requires_approval(self):
        """R64 P0-05: make_delete_file_command 构造正确,统一走审批门禁。"""
        from services.command_bus import make_delete_file_command
        cmd = make_delete_file_command(file_code="ABC123XYZ")
        assert cmd.action == "delete_file"
        assert cmd.required_permission == PERM_CONTENT_TAKEDOWN
        # R64 P0-05: delete_file 改为 requires_approval=True(集中策略统一)
        assert cmd.requires_approval is True
        # 复用 takedown 审批 action
        assert cmd.approval_action == APPROVAL_ACTION_TAKEDOWN
        assert cmd.params["file_code"] == "ABC123XYZ"
        assert cmd.handler is not None

    def test_make_enable_maintenance_command(self):
        """make_enable_maintenance_command 构造正确,需审批。"""
        cmd = make_enable_maintenance_command(reason="系统升级")
        assert cmd.action == "enable_maintenance"
        assert cmd.required_permission == PERM_MAINTENANCE_ENABLE
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_MAINTENANCE_ENABLE
        assert cmd.params["reason"] == "系统升级"

    def test_make_disable_maintenance_command(self):
        """make_disable_maintenance_command 构造正确,需审批。"""
        cmd = make_disable_maintenance_command()
        assert cmd.action == "disable_maintenance"
        assert cmd.required_permission == PERM_MAINTENANCE_DISABLE
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_MAINTENANCE_DISABLE
        assert cmd.params == {}

    def test_make_purge_data_command(self):
        """make_purge_data_command 构造正确,需审批。"""
        cmd = make_purge_data_command(table_names=["users", "file_records"])
        assert cmd.action == "purge_data"
        assert cmd.required_permission == PERM_DATA_PURGE
        assert cmd.requires_approval is True
        assert cmd.approval_action == APPROVAL_ACTION_DELETE_DATA
        assert cmd.params["table_names"] == ["users", "file_records"]

    @pytest.mark.asyncio
    async def test_takedown_handler_executes_content_reports(self, monkeypatch):
        """make_takedown_command 的 handler 正确调用 content_reports.takedown_content。"""
        import services.content_reports as cr_mod

        call_args = {"received": None}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            call_args["received"] = {
                "target_type": target_type,
                "target_id": target_id,
                "reason": reason,
                "admin_id": admin_id,
            }
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        cmd = make_takedown_command(
            target_type="file_code", target_id="FC001", reason="test reason",
        )
        result = await cmd.handler(cmd.params)

        assert result == {"takedown_ok": True}
        assert call_args["received"]["target_type"] == "file_code"
        assert call_args["received"]["target_id"] == "FC001"
        assert call_args["received"]["reason"] == "test reason"

    @pytest.mark.asyncio
    async def test_ban_user_handler_executes_content_reports(self, monkeypatch):
        """make_ban_user_command 的 handler 正确调用 content_reports.ban_user。"""
        import services.content_reports as cr_mod

        call_args = {"received": None}

        async def _fake_ban(user_id, reason, duration_days=0, admin_id=0):
            call_args["received"] = {
                "user_id": user_id,
                "reason": reason,
                "duration_days": duration_days,
                "admin_id": admin_id,
            }
            return True

        monkeypatch.setattr(cr_mod, "ban_user", _fake_ban)

        cmd = make_ban_user_command(user_id=123, reason="违规", duration_days=30)
        result = await cmd.handler(cmd.params)

        assert result == {"ban_ok": True}
        assert call_args["received"]["user_id"] == 123
        assert call_args["received"]["reason"] == "违规"
        assert call_args["received"]["duration_days"] == 30


# ════════════════════════════════════════════════════════════════
# 5. HIGH_RISK_COMMAND_REGISTRY 注册完整性测试
# ════════════════════════════════════════════════════════════════

class TestHighRiskRegistry:
    """R40 P0-8: 高风险命令注册表完整性测试。"""

    def test_registry_contains_all_required_commands(self):
        """注册表应包含所有 12 个高风险命令(含 R64 P0-05 新增 destructive 子动作)。"""
        expected_actions = {
            "takedown_report", "ban_user", "unban_user", "assign_role",
            "restore_backup", "enable_maintenance", "disable_maintenance",
            "purge_data", "delete_file",
            # R64 P0-05: 5 个 destructive 子动作统一 requires_approval=True
            "detach_file", "block_user_for_file", "restore_content",
        }
        assert set(HIGH_RISK_COMMAND_REGISTRY.keys()) == expected_actions

    def test_disaster_restore_requires_approval(self):
        """灾备恢复命令必须 requires_approval=True。"""
        perm, approval_action, requires_approval = HIGH_RISK_COMMAND_REGISTRY["restore_backup"]
        assert perm == PERM_DISASTER_RESTORE
        assert approval_action == APPROVAL_ACTION_RESTORE
        assert requires_approval is True

    def test_unban_user_requires_approval_r64_p0_5(self):
        """R64 P0-05: 解封用户(高风险逆操作)统一走审批门禁。"""
        perm, approval_action, requires_approval = HIGH_RISK_COMMAND_REGISTRY["unban_user"]
        assert perm == PERM_USERS_UNBAN
        assert requires_approval is True
        # 复用 ban 审批 action(逆操作)
        assert approval_action == APPROVAL_ACTION_BAN

    def test_destructive_subactions_require_approval_r64_p0_5(self):
        """R64 P0-05: delete_file / detach_file / block_user_for_file / restore_content 统一走审批门禁。"""
        # delete_file
        perm, approval_action, requires_approval = HIGH_RISK_COMMAND_REGISTRY["delete_file"]
        assert perm == PERM_CONTENT_TAKEDOWN
        assert requires_approval is True
        assert approval_action == APPROVAL_ACTION_TAKEDOWN
        # detach_file
        perm, approval_action, requires_approval = HIGH_RISK_COMMAND_REGISTRY["detach_file"]
        assert perm == PERM_CONTENT_TAKEDOWN
        assert requires_approval is True
        assert approval_action == APPROVAL_ACTION_TAKEDOWN
        # block_user_for_file
        perm, approval_action, requires_approval = HIGH_RISK_COMMAND_REGISTRY["block_user_for_file"]
        assert perm == PERM_CONTENT_TAKEDOWN
        assert requires_approval is True
        assert approval_action == APPROVAL_ACTION_TAKEDOWN
        # restore_content
        perm, approval_action, requires_approval = HIGH_RISK_COMMAND_REGISTRY["restore_content"]
        assert perm == PERM_DISASTER_RESTORE
        assert requires_approval is True
        assert approval_action == APPROVAL_ACTION_RESTORE

    def test_all_high_risk_commands_have_correct_permissions(self):
        """所有 requires_approval=True 的命令应有对应的 approval_action。"""
        for action, (perm, approval_action, requires_approval) in HIGH_RISK_COMMAND_REGISTRY.items():
            if requires_approval:
                assert approval_action, f"高风险命令 {action} 缺少 approval_action"
                assert perm, f"高风险命令 {action} 缺少 permission"

    @pytest.mark.asyncio
    async def test_disaster_restore_direct_call_without_approval_fails(self):
        """灾备恢复不通过审批直接调用时,应返回 approval_required 而非执行。"""
        async def _handler(params):
            return {"restore_called": True}  # 不应被调用

        command = Command(
            action="restore_backup",
            required_permission=PERM_DISASTER_RESTORE,
            handler=_handler,
            params={"backup_id": "backup_001"},
            requires_approval=True,
            approval_action=APPROVAL_ACTION_RESTORE,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")
        mock_approval = _make_mock_approval(create_approval_id=777)
        bus = CommandBus(
            rbac_module=_make_mock_rbac(has_perm=True),
            approval_module=mock_approval,
        )

        result = await bus.execute(command, principal)

        # 即使有权限,灾备恢复仍需审批
        assert result.success is False
        assert result.approval_required is True
        assert result.approval_id == 777
        # handler 不应被执行
        assert result.data is None


# ════════════════════════════════════════════════════════════════
# 6. AdminPrincipal 数据类测试
# ════════════════════════════════════════════════════════════════

class TestAdminPrincipal:
    """R40 P0-8: AdminPrincipal 身份对象测试。"""

    def test_default_source_is_web(self):
        """AdminPrincipal 默认 source 为 web。"""
        p = AdminPrincipal(id=1, name="admin")
        assert p.source == "web"

    def test_bot_principal(self):
        """Bot 来源的 principal。"""
        p = AdminPrincipal(id=12345, name="bot_admin", source="bot")
        assert p.id == 12345
        assert p.name == "bot_admin"
        assert p.source == "bot"

    def test_action_id_generation_includes_principal_id(self):
        """生成的 action_id 应包含 principal.id。"""
        p = AdminPrincipal(id=999, name="admin", source="bot")
        action_id = _generate_action_id(p, "test_action")
        assert "test_action" in action_id
        assert "999" in action_id


# ════════════════════════════════════════════════════════════════
# 7. approval_workflow 状态常量与 mark 函数测试
# ════════════════════════════════════════════════════════════════

class TestApprovalStatusConstants:
    """R40 P0-8: 审批状态常量定义测试。"""

    def test_executing_status_defined(self):
        """EXECUTING 状态常量已定义。"""
        from services.approval_workflow import APPROVAL_STATUS_EXECUTING
        assert APPROVAL_STATUS_EXECUTING == "executing"

    def test_executed_status_defined(self):
        """EXECUTED 状态常量已定义。"""
        from services.approval_workflow import APPROVAL_STATUS_EXECUTED
        assert APPROVAL_STATUS_EXECUTED == "executed"

    def test_failed_status_defined(self):
        """FAILED 状态常量已定义。"""
        from services.approval_workflow import APPROVAL_STATUS_FAILED
        assert APPROVAL_STATUS_FAILED == "failed"

    def test_new_actions_in_requiring_set(self):
        """新增的 3 个 action 应在 _ACTIONS_REQUIRING_APPROVAL 集合中。"""
        from services.approval_workflow import (
            _ACTIONS_REQUIRING_APPROVAL,
            APPROVAL_ACTION_MAINTENANCE_ENABLE,
            APPROVAL_ACTION_MAINTENANCE_DISABLE,
            APPROVAL_ACTION_RBAC_ASSIGN,
        )
        assert APPROVAL_ACTION_MAINTENANCE_ENABLE in _ACTIONS_REQUIRING_APPROVAL
        assert APPROVAL_ACTION_MAINTENANCE_DISABLE in _ACTIONS_REQUIRING_APPROVAL
        assert APPROVAL_ACTION_RBAC_ASSIGN in _ACTIONS_REQUIRING_APPROVAL


class TestApprovalMarkFunctions:
    """R40 P0-8: approval_workflow mark 函数测试。

    使用 mock cache_store 隔离 DB,验证状态机转换逻辑。
    """

    def _make_mock_store(self):
        """构造 mock cache_store,支持 transaction 上下文管理。

        transaction() 返回一个 async context manager,其 __aenter__ 返回 mock_tx。
        这样 `async with store.transaction() as tx:` 中的 tx 就是 mock_tx。
        """
        mock_store = MagicMock()
        mock_store._db = MagicMock()
        # mock_tx 是 transaction 内的执行对象
        mock_tx = AsyncMock()
        mock_tx.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        # transaction() 返回 async context manager,其 __aenter__ 返回 mock_tx
        mock_store.transaction = MagicMock(return_value=mock_tx)
        mock_tx.__aenter__.return_value = mock_tx  # async with ... as tx → mock_tx
        mock_tx.__aexit__.return_value = None
        # add_dirty_outbox 是 async 函数
        mock_store.add_dirty_outbox = AsyncMock(return_value=None)
        return mock_store, mock_tx

    @pytest.mark.asyncio
    async def test_mark_executing_transitions_approved_to_executing(self, monkeypatch):
        """mark_executing 将 APPROVED → EXECUTING。"""
        from services import approval_workflow

        mock_store, mock_tx = self._make_mock_store()
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        result = await approval_workflow.mark_executing(1)

        assert result is True
        # 验证 tx.execute 被调用(SQL 用 ? 占位符,实际值在 params 中)
        assert mock_tx.execute.call_count >= 1
        update_call = mock_tx.execute.call_args_list[0]
        sql_arg = update_call.args[0] if update_call.args else ""
        params = update_call.args[1] if len(update_call.args) > 1 else ()
        # SQL 应包含 UPDATE approvals SET status
        assert "UPDATE approvals SET status" in sql_arg
        # WHERE status = ? 守卫(防止非 approved 状态被转换)
        assert "WHERE id = ? AND status = ?" in sql_arg
        # params 中第一个值应为 'executing'(新状态),最后一个为 'approved'(守卫旧状态)
        assert "executing" in str(params)
        assert "approved" in str(params)

    @pytest.mark.asyncio
    async def test_mark_executing_wrong_status_fails(self, monkeypatch):
        """mark_executing 状态非 approved 时返回 False(rowcount=0)。"""
        from services import approval_workflow

        mock_store = MagicMock()
        mock_store._db = MagicMock()
        mock_tx = AsyncMock()
        # 模拟 rowcount=0(状态不匹配,未更新)
        mock_tx.execute = AsyncMock(return_value=MagicMock(rowcount=0))
        mock_tx.__aenter__.return_value = mock_tx  # async with ... as tx → mock_tx
        mock_tx.__aexit__.return_value = None
        mock_store.transaction = MagicMock(return_value=mock_tx)
        mock_store.add_dirty_outbox = AsyncMock(return_value=None)
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        result = await approval_workflow.mark_executing(1)

        assert result is False

    @pytest.mark.asyncio
    async def test_mark_executed_transitions_executing_to_executed(self, monkeypatch):
        """mark_executed 将 EXECUTING → EXECUTED。"""
        from services import approval_workflow

        mock_store, mock_tx = self._make_mock_store()
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        result = await approval_workflow.mark_executed(1)

        assert result is True
        # 验证 SQL 和 params
        update_call = mock_tx.execute.call_args_list[0]
        sql_arg = update_call.args[0] if update_call.args else ""
        params = update_call.args[1] if len(update_call.args) > 1 else ()
        assert "UPDATE approvals SET status" in sql_arg
        assert "WHERE id = ? AND status = ?" in sql_arg
        # params 应包含 'executed'(新状态)和 'executing'(守卫旧状态)
        assert "executed" in str(params)
        assert "executing" in str(params)

    @pytest.mark.asyncio
    async def test_mark_failed_transitions_executing_to_failed(self, monkeypatch):
        """mark_failed 将 EXECUTING → FAILED,错误信息写入 approver_note。"""
        from services import approval_workflow

        mock_store, mock_tx = self._make_mock_store()
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        result = await approval_workflow.mark_failed(1, error="handler 执行异常")

        assert result is True
        update_call = mock_tx.execute.call_args_list[0]
        sql_arg = update_call.args[0] if update_call.args else ""
        params = update_call.args[1] if len(update_call.args) > 1 else ()
        assert "UPDATE approvals SET status" in sql_arg
        assert "WHERE id = ? AND status = ?" in sql_arg
        # params 应包含 'failed'(新状态)和 'executing'(守卫旧状态)
        assert "failed" in str(params)
        assert "executing" in str(params)
        # 验证错误信息参数包含 "ERROR: " 前缀
        assert any("ERROR:" in str(p) for p in params), \
            f"错误信息应包含 'ERROR: ' 前缀,实际参数: {params}"

    @pytest.mark.asyncio
    async def test_mark_failed_empty_error_uses_default(self, monkeypatch):
        """mark_failed 空 error 时使用默认错误信息。"""
        from services import approval_workflow

        mock_store, mock_tx = self._make_mock_store()
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        result = await approval_workflow.mark_failed(1, error="")

        assert result is True
        update_call = mock_tx.execute.call_args_list[0]
        params = update_call.args[1] if len(update_call.args) > 1 else ()
        assert any("ERROR: unknown" in str(p) for p in params), \
            f"空 error 时应使用 'ERROR: unknown',实际参数: {params}"


# ════════════════════════════════════════════════════════════════
# 8. RBAC 权限常量测试
# ════════════════════════════════════════════════════════════════

class TestRbacPermissionConstants:
    """R40 P0-8: RBAC 新增权限常量测试。"""

    def test_new_permission_constants_defined(self):
        """新增的 8 个 CommandBus 权限常量已定义。"""
        from services import rbac

        assert rbac.PERMISSION_CONTENT_TAKEDOWN == "content:takedown"
        assert rbac.PERMISSION_USERS_BAN == "users:ban"
        assert rbac.PERMISSION_USERS_UNBAN == "users:unban"
        assert rbac.PERMISSION_RBAC_ASSIGN == "rbac:assign"
        assert rbac.PERMISSION_DISASTER_RESTORE == "disaster:restore"
        assert rbac.PERMISSION_MAINTENANCE_ENABLE == "maintenance:enable"
        assert rbac.PERMISSION_MAINTENANCE_DISABLE == "maintenance:disable"
        assert rbac.PERMISSION_DATA_PURGE == "data:purge"

    def test_security_role_has_command_bus_permissions(self):
        """security 角色应包含 CommandBus 内容下架/封禁/分配角色权限。"""
        from services import rbac

        security_perms = rbac._DEFAULT_ROLE_PERMISSIONS[rbac.ROLE_SECURITY]
        assert rbac.PERMISSION_CONTENT_TAKEDOWN in security_perms
        assert rbac.PERMISSION_USERS_BAN in security_perms
        assert rbac.PERMISSION_USERS_UNBAN in security_perms
        assert rbac.PERMISSION_RBAC_ASSIGN in security_perms

    def test_ops_role_has_command_bus_permissions(self):
        """ops 角色应包含维护模式/灾备恢复/数据清除权限。"""
        from services import rbac

        ops_perms = rbac._DEFAULT_ROLE_PERMISSIONS[rbac.ROLE_OPS]
        assert rbac.PERMISSION_MAINTENANCE_ENABLE in ops_perms
        assert rbac.PERMISSION_MAINTENANCE_DISABLE in ops_perms
        assert rbac.PERMISSION_DISASTER_RESTORE in ops_perms
        assert rbac.PERMISSION_DATA_PURGE in ops_perms

    def test_operator_role_has_ban_permissions(self):
        """operator 角色应包含封禁/解封权限。"""
        from services import rbac

        operator_perms = rbac._DEFAULT_ROLE_PERMISSIONS[rbac.ROLE_OPERATOR]
        assert rbac.PERMISSION_USERS_BAN in operator_perms
        assert rbac.PERMISSION_USERS_UNBAN in operator_perms

    @pytest.mark.asyncio
    async def test_list_permissions_returns_all_defined(self):
        """list_permissions 返回所有已定义权限(含新增 CommandBus 权限)。"""
        from services import rbac

        perms = await rbac.list_permissions()
        perm_names = {p["name"] for p in perms}
        # 新增 CommandBus 权限应在列表中
        assert rbac.PERMISSION_RBAC_ASSIGN in perm_names
        assert rbac.PERMISSION_DISASTER_RESTORE in perm_names
        assert rbac.PERMISSION_MAINTENANCE_ENABLE in perm_names
        assert rbac.PERMISSION_DATA_PURGE in perm_names


# ════════════════════════════════════════════════════════════════
# 9. 审批通过后自动执行集成测试
# ════════════════════════════════════════════════════════════════

class TestApproveAutoExecution:
    """R40 P0-8 + R41 P0-4: approve() 写入 command_outbox 集成测试。

    R41 P0-4 变更:
        approve() 不再直接调用 CommandBus.execute_approved_action() 执行 handler,
        而是将命令写入 ``command_outbox`` 表(独立事务),由 ApprovalExecutor 异步消费。
        这样可消除嵌套 SQLite transaction(BEGIN within BEGIN)的风险。
    """

    @pytest.mark.asyncio
    async def test_approve_writes_command_outbox_for_commandbus_approval(self, monkeypatch):
        """approve() 对 CommandBus 创建的审批(含 command_action)写入 command_outbox。

        验证:
        - approve() 返回 True(审批已批准)
        - takedown_content 不被直接调用(由 ApprovalExecutor 异步消费)
        - mock_tx.execute 至少一次调用包含 INSERT INTO command_outbox SQL
        """
        from services import approval_workflow
        import services.content_reports as cr_mod

        # Mock content_reports.takedown_content(用于断言未被调用)
        takedown_called = {"n": 0}

        async def _fake_takedown(target_type, target_id, reason, admin_id):
            takedown_called["n"] += 1
            return True

        monkeypatch.setattr(cr_mod, "takedown_content", _fake_takedown)

        # Mock get_cache_store 提供 transaction
        mock_store = MagicMock()
        mock_store._db = MagicMock()
        mock_tx = AsyncMock()
        mock_tx.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        # transaction() 返回 mock_tx,且 __aenter__ 返回 mock_tx 自身
        # (确保 `async with store.transaction() as tx:` 中的 tx 是 mock_tx)
        mock_store.transaction = MagicMock(return_value=mock_tx)
        mock_tx.__aenter__.return_value = mock_tx
        mock_tx.__aexit__.return_value = None
        mock_store.add_dirty_outbox = AsyncMock(return_value=None)
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        # Mock get_approval 返回 pending 状态的审批(含 command_action payload)
        approval_record = {
            "id": 1,
            "action": APPROVAL_ACTION_TAKEDOWN,
            "payload": {
                "command_action": "takedown_report",
                "params": {"target_type": "file_code", "target_id": "TC001", "reason": ""},
                "principal_id": 100,
                "principal_name": "creator",
                "principal_source": "bot",
                "action_id": "integration_test_001",
            },
            "status": "pending",
            "approver_id": None,
            "approver_note": "",
            "created_by": 100,  # 创建者 != 审批人
            "created_at": "2026-07-13T10:00:00",
            "resolved_at": None,
        }

        monkeypatch.setattr(
            approval_workflow, "get_approval",
            AsyncMock(return_value=approval_record),
        )

        # Mock rbac.check_permission 让审批人有权限
        monkeypatch.setattr(
            approval_workflow, "check_permission",
            AsyncMock(return_value=True),
        )

        # 执行 approve
        ok = await approval_workflow.approve(
            approval_id=1, approver_id=200, note="approved",
        )

        # approve 应成功(已批准)
        assert ok is True
        # R41 P0-4: handler 不应被直接调用(由 ApprovalExecutor 异步消费)
        assert takedown_called["n"] == 0, \
            "approve() 不应直接调用 takedown_content,应通过 command_outbox 异步消费"
        # 验证至少一次 INSERT INTO command_outbox 调用
        insert_calls = [
            c for c in mock_tx.execute.call_args_list
            if c.args and "INSERT INTO command_outbox" in str(c.args[0])
        ]
        assert len(insert_calls) >= 1, \
            "approve() 应通过 mock_tx.execute 写入 command_outbox"

    @pytest.mark.asyncio
    async def test_approve_skips_auto_execution_for_non_commandbus_approval(self, monkeypatch):
        """approve() 对非 CommandBus 创建的审批(无 command_action)不写 command_outbox。"""
        from services import approval_workflow

        # Mock get_cache_store
        mock_store = MagicMock()
        mock_store._db = MagicMock()
        mock_tx = AsyncMock()
        mock_tx.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        mock_store.transaction = MagicMock(return_value=mock_tx)
        mock_store.add_dirty_outbox = AsyncMock(return_value=None)
        monkeypatch.setattr(approval_workflow, "get_cache_store", lambda: mock_store)

        # 审批记录不含 command_action(非 CommandBus 创建)
        approval_record = {
            "id": 2,
            "action": "config_change",
            "payload": {"key": "file_prefix", "value": "test"},  # 无 command_action
            "status": "pending",
            "approver_id": None,
            "approver_note": "",
            "created_by": 100,
            "created_at": "2026-07-13T10:00:00",
            "resolved_at": None,
        }

        monkeypatch.setattr(
            approval_workflow, "get_approval",
            AsyncMock(return_value=approval_record),
        )
        monkeypatch.setattr(
            approval_workflow, "check_permission",
            AsyncMock(return_value=True),
        )

        # 执行 approve(不应触发 CommandBus)
        ok = await approval_workflow.approve(
            approval_id=2, approver_id=200, note="ok",
        )

        assert ok is True
        # 由于无 command_action,不会写 command_outbox
        insert_calls = [
            c for c in mock_tx.execute.call_args_list
            if c.args and "INSERT INTO command_outbox" in str(c.args[0])
        ]
        assert len(insert_calls) == 0, \
            "无 command_action 时不应写 command_outbox"


# ════════════════════════════════════════════════════════════════
# 10. Result 数据类测试
# ════════════════════════════════════════════════════════════════

class TestResultDataclass:
    """R40 P0-8: Result 数据类测试。"""

    def test_success_result_defaults(self):
        """成功结果默认值正确。"""
        r = Result(success=True)
        assert r.success is True
        assert r.data is None
        assert r.error == ""
        assert r.approval_id == 0
        assert r.approval_required is False
        assert r.action_id == ""

    def test_failure_result_with_error(self):
        """失败结果携带 error。"""
        r = Result(success=False, error="权限不足")
        assert r.success is False
        assert r.error == "权限不足"

    def test_approval_required_result(self):
        """需审批结果携带 approval_id。"""
        r = Result(
            success=False,
            approval_id=42,
            approval_required=True,
            error="操作需要审批(approval_id=42)",
        )
        assert r.approval_required is True
        assert r.approval_id == 42
        assert "42" in r.error
