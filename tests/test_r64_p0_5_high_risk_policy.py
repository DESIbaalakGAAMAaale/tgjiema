"""R64 P0-05: 高风险操作策略统一测试。

终审整改需求:
    1. 建立单一 ``HighRiskPolicy``: action → required_role、MFA、two_person、reason、
       resource_version、cooldown、reversible、outbox_effects。
    2. delete / ban / detach / block / restore / purge / 密钥轮换 / 权限变更 默认
       MFA + requester != approver + resource version CAS。
    3. callback token 必须绑定 tenant、actor、audience、exact action、sub_action、
       resource id、resource version、locale、session id、expiry、nonce。
    4. handler 不再自行决定风险级别,只能构造命令并交给 policy/CommandBus。
    5. 对"取消/忽略"等低风险按钮也绑定 actor/session/resource,防止跨会话误操作。

测试覆盖:
    A. HighRiskPolicy 表完整性
        - 所有 destructive action 都在 HIGH_RISK_POLICY 中
        - 所有 destructive action 都 requires_mfa=True + requires_two_person=True
        - 所有 destructive action 都 requires_resource_version=True(系统级除外)
        - 查询接口正确性(get_policy / is_high_risk / requires_mfa / 等)
    B. CommandBus 集成
        - 5 个此前 requires_approval=False 的 destructive action 现在为 True
        - HIGH_RISK_COMMAND_REGISTRY 包含 12 个 action
        - _resolve_command_for_action 支持 detach_file / block_user_for_file
    C. button_approval_policy 双人审批加固
        - ButtonApprovalContext 新增 requires_mfa / requires_two_person 字段
        - requires_two_person=True 时 approver 必须 MFA(approver_mfa_verified)
        - 名义双人审批(approver_id != principal_id 但无 approver MFA)被拒绝
    D. button_security v2 签名
        - v2 token 含 sub_action / session_id / locale 进入签名
        - v2 verify 提取 sub_action / session_id / locale
        - v2 handle 绑定 sub_action / session_id / locale 强制匹配
        - v1 函数向后兼容(签名格式不变)
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ── 模块级 skip 检查:cache_store 必须是真实类(非 conftest 降级 MagicMock)──
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。"""
    tmpdir = tempfile.mkdtemp(prefix="r64_p0_5_test_")
    db_path = Path(tmpdir) / "test_r64_p0_5.db"
    original_path = _cs_module.DB_PATH
    original_get_store = _cs_module.get_cache_store
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module.get_cache_store = lambda: s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module.get_cache_store = original_get_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def setup_bot_token(monkeypatch):
    """为 button_security 提供固定 BOT_TOKEN(避免 MagicMock 导致 HMAC 失败)。"""
    import config
    monkeypatch.setattr(config.settings, "ADMIN_BOT_TOKEN", "r64_p0_5_test_admin_bot_token")
    monkeypatch.setattr(config.settings, "SENDER_BOT_TOKEN", "r64_p0_5_test_sender_bot_token")
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(config.settings, "ADMIN_TELEGRAM_ID", "999999")


# ════════════════════════════════════════════════════════════════
# A. HighRiskPolicy 表完整性
# ════════════════════════════════════════════════════════════════


class TestHighRiskPolicyTable:
    """R64 P0-05: HIGH_RISK_POLICY 表完整性测试。"""

    def test_all_destructive_actions_in_policy(self):
        """所有 destructive action 必须在 HIGH_RISK_POLICY 中。"""
        from services.high_risk_policy import HIGH_RISK_POLICY

        expected_destructive = {
            "delete_file", "detach_file", "block_user_for_file",
            "ban_user", "unban_user", "takedown_report", "restore_content",
            "restore_backup", "purge_data", "assign_role", "rotate_keys",
            "enable_maintenance", "disable_maintenance",
        }
        assert expected_destructive.issubset(set(HIGH_RISK_POLICY.keys())), (
            f"缺少 destructive action: {expected_destructive - set(HIGH_RISK_POLICY.keys())}"
        )

    def test_all_destructive_actions_require_mfa(self):
        """R64 P0-05: 所有 destructive action 必须 requires_mfa=True。"""
        from services.high_risk_policy import HIGH_RISK_POLICY

        no_mfa = [
            action for action, rule in HIGH_RISK_POLICY.items()
            if not rule.requires_mfa
        ]
        assert not no_mfa, (
            f"以下 destructive action 未启用 MFA: {no_mfa}"
        )

    def test_all_destructive_actions_require_two_person(self):
        """R64 P0-05: 所有 destructive action 必须 requires_two_person=True。"""
        from services.high_risk_policy import HIGH_RISK_POLICY

        no_two_person = [
            action for action, rule in HIGH_RISK_POLICY.items()
            if not rule.requires_two_person
        ]
        assert not no_two_person, (
            f"以下 destructive action 未启用 two_person: {no_two_person}"
        )

    def test_destructive_actions_require_resource_version(self):
        """R64 P0-05: delete/ban/detach/block/restore/purge 等需要 resource_version CAS。

        注:enable_maintenance / disable_maintenance 为系统级状态,无 resource version,
        允许 requires_resource_version=False。
        """
        from services.high_risk_policy import HIGH_RISK_POLICY

        # 系统级 action 不需要 resource version(无具体资源)
        system_level_actions = {"enable_maintenance", "disable_maintenance"}
        for action, rule in HIGH_RISK_POLICY.items():
            if action in system_level_actions:
                continue
            assert rule.requires_resource_version is True, (
                f"action={action} 应启用 resource_version CAS"
            )

    def test_get_policy_returns_rule_for_known_action(self):
        """get_policy 对已知 action 返回 HighRiskRule。"""
        from services.high_risk_policy import get_policy, HighRiskRule

        rule = get_policy("delete_file")
        assert rule is not None
        assert isinstance(rule, HighRiskRule)
        assert rule.action == "delete_file"
        assert rule.requires_mfa is True
        assert rule.requires_two_person is True

    def test_get_policy_returns_none_for_unknown_action(self):
        """get_policy 对未知 action 返回 None。"""
        from services.high_risk_policy import get_policy

        assert get_policy("view") is None
        assert get_policy("low_risk_action") is None
        assert get_policy("") is None

    def test_is_high_risk_true_for_destructive(self):
        """is_high_risk 对 destructive action 返回 True。"""
        from services.high_risk_policy import is_high_risk

        for action in ("delete_file", "ban_user", "unban_user", "purge_data"):
            assert is_high_risk(action) is True, f"action={action} 应为高风险"

    def test_is_high_risk_false_for_low_risk(self):
        """is_high_risk 对低风险 action 返回 False。"""
        from services.high_risk_policy import is_high_risk

        assert is_high_risk("view") is False
        assert is_high_risk("cancel") is False
        assert is_high_risk("") is False

    # ════════════════════════════════════════════════════════════
    # R65 P0-05: destructive namespace fail-closed 测试
    # ════════════════════════════════════════════════════════════

    def test_r65_get_policy_fails_closed_for_unregistered_destructive(self):
        """R65 P0-05: get_policy 对未注册的 destructive action 抛
        HIGH_RISK_ACTION_UNREGISTERED(fail-closed)。

        覆盖各种 destructive 关键词:delete/purge/ban/block/takedown/detach/
        restore/reset/rotate/assign/revoke/grant/enable/disable/wipe/clear/
        shutdown/restart/factory_reset/break_glass/force_logout 等。
        """
        import pytest
        from services.high_risk_policy import get_policy
        from services.error_codes import AppError, ErrorCodes

        # 各种未注册但属于 destructive namespace 的 action
        unregistered_destructive = [
            "delete_widget",          # delete 关键词
            "purge_logs",             # purge 关键词
            "ban_ip",                 # ban 关键词
            "block_account",          # block 关键词
            "takedown_user",          # takedown 关键词
            "detach_relation",        # detach 关键词
            "restore_snapshot",       # restore 关键词
            "reset_password",         # reset 关键词
            "rotate_credentials",     # rotate 关键词
            "assign_permission",      # assign 关键词
            "revoke_token",           # revoke 关键词
            "grant_role",             # grant 关键词
            "enable_feature",         # enable 关键词
            "disable_service",        # disable 关键词
            "wipe_disk",              # wipe 关键词
            "clear_cache_permanent",  # clear 关键词
            "shutdown_node",          # shutdown 关键词
            "restart_service",        # restart 关键词
            "factory_reset_node",     # factory_reset 关键词
            "break_glass_mode",       # break_glass 关键词
            "force_logout_user",      # force_logout 关键词
            "approve_appeal_x",       # approve_appeal 关键词
            "reject_appeal_y",        # reject_appeal 关键词
            "update_config_db",       # update_config 关键词
            "reload_config_now",      # reload_config 关键词
            "drop_table_x",           # drop 关键词
            "truncate_logs",          # truncate 关键词
            "destroy_evidence",       # destroy 关键词
            "scrub_data",             # scrub 关键词
            "kick_user",              # kick 关键词
            "remove_member",          # remove 关键词
            "recover_lost",           # recover 关键词
        ]
        for action in unregistered_destructive:
            with pytest.raises(AppError) as exc_info:
                get_policy(action)
            assert exc_info.value.code == ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED, (
                f"action={action} 应抛 HIGH_RISK_ACTION_UNREGISTERED, "
                f"实际抛出 code={exc_info.value.code}"
            )

    def test_r65_is_high_risk_fails_closed_for_unregistered_destructive(self):
        """R65 P0-05: is_high_risk 对未注册的 destructive action 抛
        HIGH_RISK_ACTION_UNREGISTERED(fail-closed)。"""
        import pytest
        from services.high_risk_policy import is_high_risk
        from services.error_codes import AppError, ErrorCodes

        for action in ("delete_widget", "purge_logs", "ban_ip", "reset_password"):
            with pytest.raises(AppError) as exc_info:
                is_high_risk(action)
            assert exc_info.value.code == ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED

    def test_r65_requires_mfa_fails_closed_for_unregistered_destructive(self):
        """R65 P0-05: requires_mfa 对未注册的 destructive action fail-closed。"""
        import pytest
        from services.high_risk_policy import requires_mfa
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            requires_mfa("delete_widget")
        assert exc_info.value.code == ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED

    def test_r65_requires_two_person_fails_closed_for_unregistered_destructive(self):
        """R65 P0-05: requires_two_person 对未注册的 destructive action fail-closed。"""
        import pytest
        from services.high_risk_policy import requires_two_person
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            requires_two_person("purge_logs")
        assert exc_info.value.code == ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED

    def test_r65_requires_resource_version_fails_closed_for_unregistered_destructive(self):
        """R65 P0-05: requires_resource_version 对未注册的 destructive action fail-closed。"""
        import pytest
        from services.high_risk_policy import requires_resource_version
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            requires_resource_version("ban_ip")
        assert exc_info.value.code == ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED

    def test_r65_get_required_role_fails_closed_for_unregistered_destructive(self):
        """R65 P0-05: get_required_role 对未注册的 destructive action fail-closed。"""
        import pytest
        from services.high_risk_policy import get_required_role
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            get_required_role("reset_password")
        assert exc_info.value.code == ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED

    def test_r65_get_policy_returns_none_for_safe_actions(self):
        """R65 P0-05: 非 destructive action 仍返回 None(只读/查询类操作)。"""
        from services.high_risk_policy import get_policy

        # 各种只读/查询类 action,不应触发 fail-closed
        safe_actions = [
            "view", "cancel", "refresh", "list", "get", "query", "read",
            "search", "ping", "readiness", "health_check", "export_status",
            "status", "info", "describe", "show", "low_risk_action", "",
            "get_user", "list_files", "search_posts", "view_settings",
            "describe_cluster", "show_config",
        ]
        for action in safe_actions:
            assert get_policy(action) is None, (
                f"action={action} 是非 destructive action,应返回 None"
            )

    def test_r65_is_high_risk_returns_false_for_safe_actions(self):
        """R65 P0-05: 非 destructive action 仍返回 False(只读/查询类操作)。"""
        from services.high_risk_policy import is_high_risk

        for action in ("view", "cancel", "refresh", "list", "get", "query", ""):
            assert is_high_risk(action) is False, (
                f"action={action} 是非 destructive action,应返回 False"
            )

    def test_r65_destructive_keywords_complete(self):
        """R65 P0-05: DESTRUCTIVE_ACTION_KEYWORDS 覆盖所有预期的关键词。"""
        from services.high_risk_policy import DESTRUCTIVE_ACTION_KEYWORDS

        expected_keywords = {
            "delete", "purge", "drop", "truncate", "destroy", "wipe", "clear", "scrub",
            "ban", "unban", "block", "kick",
            "takedown", "detach", "remove",
            "restore", "recover",
            "reset", "rotate",
            "assign", "revoke", "grant",
            "enable", "disable",
            "factory_reset", "break_glass", "force_logout",
            "approve_appeal", "reject_appeal",
            "update_config", "reload_config",
            "shutdown", "restart",
        }
        actual_keywords = set(DESTRUCTIVE_ACTION_KEYWORDS)
        missing = expected_keywords - actual_keywords
        assert not missing, f"DESTRUCTIVE_ACTION_KEYWORDS 缺少: {missing}"

    def test_requires_mfa_query_function(self):
        """requires_mfa 查询接口正确性。"""
        from services.high_risk_policy import requires_mfa

        assert requires_mfa("delete_file") is True
        assert requires_mfa("ban_user") is True
        assert requires_mfa("view") is False  # 非高风险

    def test_requires_two_person_query_function(self):
        """requires_two_person 查询接口正确性。"""
        from services.high_risk_policy import requires_two_person

        assert requires_two_person("delete_file") is True
        assert requires_two_person("unban_user") is True
        assert requires_two_person("view") is False

    def test_requires_resource_version_query_function(self):
        """requires_resource_version 查询接口正确性。"""
        from services.high_risk_policy import requires_resource_version

        assert requires_resource_version("delete_file") is True
        assert requires_resource_version("ban_user") is True
        # 系统级 action 不需要 resource version
        assert requires_resource_version("enable_maintenance") is False
        assert requires_resource_version("disable_maintenance") is False
        # 非高风险
        assert requires_resource_version("view") is False

    def test_get_required_role_returns_correct_permission(self):
        """get_required_role 返回正确的 RBAC 权限标识。"""
        from services.high_risk_policy import get_required_role
        from services.command_bus import (
            PERM_CONTENT_TAKEDOWN, PERM_USERS_BAN, PERM_USERS_UNBAN,
            PERM_DISASTER_RESTORE, PERM_DATA_PURGE,
        )

        assert get_required_role("delete_file") == PERM_CONTENT_TAKEDOWN
        assert get_required_role("ban_user") == PERM_USERS_BAN
        assert get_required_role("unban_user") == PERM_USERS_UNBAN
        assert get_required_role("restore_content") == PERM_DISASTER_RESTORE
        assert get_required_role("purge_data") == PERM_DATA_PURGE
        assert get_required_role("view") is None  # 非高风险

    def test_5_formerly_false_actions_now_require_mfa_and_two_person(self):
        """R64 P0-05 关键断言: 此前 requires_approval=False 的 5 个 destructive action
        现在都 requires_mfa=True + requires_two_person=True。"""
        from services.high_risk_policy import (
            HIGH_RISK_POLICY, requires_mfa, requires_two_person, is_high_risk,
        )

        formerly_false_actions = [
            "unban_user", "delete_file", "detach_file",
            "block_user_for_file", "restore_content",
        ]
        for action in formerly_false_actions:
            assert action in HIGH_RISK_POLICY, (
                f"action={action} 必须在 HIGH_RISK_POLICY 中(R64 P0-05 整改)"
            )
            assert is_high_risk(action) is True, (
                f"action={action} 必须 is_high_risk=True"
            )
            assert requires_mfa(action) is True, (
                f"action={action} 必须 requires_mfa=True"
            )
            assert requires_two_person(action) is True, (
                f"action={action} 必须 requires_two_person=True"
            )


# ════════════════════════════════════════════════════════════════
# B. CommandBus 集成
# ════════════════════════════════════════════════════════════════


class TestCommandBusHighRiskPolicyIntegration:
    """R64 P0-05: CommandBus 工厂函数集成 HighRiskPolicy。"""

    def test_make_unban_user_command_requires_approval(self):
        """R64 P0-05: make_unban_user_command 现在 requires_approval=True。"""
        from services.command_bus import make_unban_user_command, APPROVAL_ACTION_BAN

        cmd = make_unban_user_command(user_id=12345)
        assert cmd.action == "unban_user"
        assert cmd.requires_approval is True, (
            "R64 P0-05: unban_user 必须 requires_approval=True"
        )
        assert cmd.approval_action == APPROVAL_ACTION_BAN

    def test_make_delete_file_command_requires_approval(self):
        """R64 P0-05: make_delete_file_command 现在 requires_approval=True。"""
        from services.command_bus import (
            make_delete_file_command, APPROVAL_ACTION_TAKEDOWN,
        )

        cmd = make_delete_file_command(file_code="ABC123")
        assert cmd.action == "delete_file"
        assert cmd.requires_approval is True, (
            "R64 P0-05: delete_file 必须 requires_approval=True"
        )
        assert cmd.approval_action == APPROVAL_ACTION_TAKEDOWN

    def test_make_detach_file_command_requires_approval(self):
        """R64 P0-05: make_detach_file_command 现在 requires_approval=True。"""
        from services.command_bus import (
            make_detach_file_command, APPROVAL_ACTION_TAKEDOWN,
        )

        cmd = make_detach_file_command(file_code="ABC123", reason="report:detach")
        assert cmd.action == "detach_file"
        assert cmd.requires_approval is True, (
            "R64 P0-05: detach_file 必须 requires_approval=True"
        )
        assert cmd.approval_action == APPROVAL_ACTION_TAKEDOWN

    def test_make_block_user_for_file_command_requires_approval(self):
        """R64 P0-05: make_block_user_for_file_command 现在 requires_approval=True。"""
        from services.command_bus import (
            make_block_user_for_file_command, APPROVAL_ACTION_TAKEDOWN,
        )

        cmd = make_block_user_for_file_command(
            file_code="ABC123", user_id=67890, reason="report:block",
        )
        assert cmd.action == "block_user_for_file"
        assert cmd.requires_approval is True, (
            "R64 P0-05: block_user_for_file 必须 requires_approval=True"
        )
        assert cmd.approval_action == APPROVAL_ACTION_TAKEDOWN

    def test_make_restore_content_command_requires_approval(self):
        """R64 P0-05: make_restore_content_command 现在 requires_approval=True。"""
        from services.command_bus import (
            make_restore_content_command, APPROVAL_ACTION_RESTORE,
        )

        cmd = make_restore_content_command(
            appeal_id=1, target_type="file", target_id="ABC123",
            admin_id=100, content_hash="hash123",
        )
        assert cmd.action == "restore_content"
        assert cmd.requires_approval is True, (
            "R64 P0-05: restore_content 必须 requires_approval=True"
        )
        assert cmd.approval_action == APPROVAL_ACTION_RESTORE

    def test_high_risk_command_registry_contains_12_actions(self):
        """R64 P0-05: HIGH_RISK_COMMAND_REGISTRY 包含 12 个 action。"""
        from services.command_bus import HIGH_RISK_COMMAND_REGISTRY

        expected = {
            "takedown_report", "ban_user", "unban_user", "assign_role",
            "restore_backup", "enable_maintenance", "disable_maintenance",
            "purge_data", "delete_file",
            # R64 P0-05 新增 3 个
            "detach_file", "block_user_for_file", "restore_content",
        }
        assert set(HIGH_RISK_COMMAND_REGISTRY.keys()) == expected

    def test_high_risk_command_registry_all_true(self):
        """R64 P0-05: HIGH_RISK_COMMAND_REGISTRY 中所有 action requires_approval=True。"""
        from services.command_bus import HIGH_RISK_COMMAND_REGISTRY

        for action, (_, _, requires_approval) in HIGH_RISK_COMMAND_REGISTRY.items():
            assert requires_approval is True, (
                f"action={action} 必须 requires_approval=True(R64 P0-05 统一)"
            )

    def test_resolve_command_supports_detach_file(self):
        """R64 P0-05: _resolve_command_for_action 支持 detach_file。"""
        from services.command_bus import _resolve_command_for_action

        cmd = _resolve_command_for_action(
            "detach_file",
            {"file_code": "ABC123", "reason": "report:detach"},
        )
        assert cmd is not None
        assert cmd.action == "detach_file"

    def test_resolve_command_supports_block_user_for_file(self):
        """R64 P0-05: _resolve_command_for_action 支持 block_user_for_file。"""
        from services.command_bus import _resolve_command_for_action

        cmd = _resolve_command_for_action(
            "block_user_for_file",
            {"file_code": "ABC123", "user_id": 67890, "reason": "report:block"},
        )
        assert cmd is not None
        assert cmd.action == "block_user_for_file"

    @pytest.mark.asyncio
    async def test_execute_writes_high_risk_policy_metadata_to_approval(self):
        """R64 P0-05: CommandBus.execute() 将 HighRiskPolicy 元数据写入 approval payload。"""
        from services.command_bus import (
            Command, CommandBus, AdminPrincipal,
            PERM_CONTENT_TAKEDOWN, APPROVAL_ACTION_TAKEDOWN,
        )

        async def _handler(params):
            return {"ok": True}

        command = Command(
            action="delete_file",
            required_permission=PERM_CONTENT_TAKEDOWN,
            handler=_handler,
            params={"file_code": "ABC123"},
            requires_approval=True,
            approval_action=APPROVAL_ACTION_TAKEDOWN,
        )
        principal = AdminPrincipal(id=1, name="admin", source="bot")

        # mock rbac + approval
        mock_rbac = MagicMock()
        mock_rbac.check_permission = AsyncMock(return_value=True)

        captured_payload = {}

        async def _capture_create_approval(action, payload, created_by):
            captured_payload.update(payload)
            return 42

        mock_approval = MagicMock()
        mock_approval.create_approval = _capture_create_approval

        bus = CommandBus(rbac_module=mock_rbac, approval_module=mock_approval)
        result = await bus.execute(command, principal)

        # 应创建审批(不直接执行)
        assert result.success is False
        assert result.approval_required is True
        assert result.approval_id == 42

        # R64 P0-05: payload 必须包含 high_risk_policy 元数据
        assert "high_risk_policy" in captured_payload, (
            "approval payload 必须包含 high_risk_policy 元数据(R64 P0-05)"
        )
        policy_meta = captured_payload["high_risk_policy"]
        assert policy_meta is not None
        assert policy_meta["requires_mfa"] is True
        assert policy_meta["requires_two_person"] is True
        assert policy_meta["requires_resource_version"] is True


# ════════════════════════════════════════════════════════════════
# C. button_approval_policy 双人审批加固
# ════════════════════════════════════════════════════════════════


class TestButtonApprovalContextR64Fields:
    """R64 P0-05: ButtonApprovalContext 新增字段测试。"""

    def test_context_accepts_requires_mfa_field(self):
        """ButtonApprovalContext 接受 requires_mfa 字段。"""
        from services.button_approval_policy import ButtonApprovalContext

        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=9999999999,
            nonce="nonce123", signature="a" * 32,
            requires_mfa=True,
        )
        assert ctx.requires_mfa is True

    def test_context_accepts_requires_two_person_field(self):
        """ButtonApprovalContext 接受 requires_two_person 字段。"""
        from services.button_approval_policy import ButtonApprovalContext

        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=9999999999,
            nonce="nonce123", signature="a" * 32,
            requires_two_person=True,
        )
        assert ctx.requires_two_person is True

    def test_context_accepts_approver_mfa_verified_field(self):
        """ButtonApprovalContext 接受 approver_mfa_verified 字段。"""
        from services.button_approval_policy import ButtonApprovalContext

        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=9999999999,
            nonce="nonce123", signature="a" * 32,
            requires_two_person=True,
            approver_id=2002,
            approver_mfa_verified=True,
        )
        assert ctx.approver_mfa_verified is True

    def test_context_defaults_new_fields_to_false(self):
        """R64 P0-05: 新字段默认 False(向后兼容)。"""
        from services.button_approval_policy import ButtonApprovalContext

        ctx = ButtonApprovalContext(
            action="ban", principal_id=1001,
            resource="user:2001", resource_version="v1",
            request_hash="abc", expiry_ts=9999999999,
            nonce="nonce123", signature="a" * 32,
        )
        assert ctx.requires_mfa is False
        assert ctx.requires_two_person is False
        assert ctx.approver_mfa_verified is False
        assert ctx.approver_mfa_receipt is None


class TestDualApprovalEnhanced:
    """R64 P0-05: requires_two_person=True 时,approver 必须独立 MFA。"""

    @pytest.mark.asyncio
    async def test_two_person_requires_approver_mfa(self, store, monkeypatch):
        """R64 P0-05: requires_two_person=True + approver_mfa_verified=False → 拒绝。

        此前仅校验 approver_id != principal_id,是"名义双人审批"。
        现在审批人也必须 MFA,防止单管理员账号同时充当 requester/approver。
        """
        from services.button_approval_policy import (
            ButtonApprovalContext, enforce_button_approval_policy,
        )
        from services.button_security import sign_button_token_with_nonce
        from services.error_codes import AppError, ErrorCodes

        # 构造一个合法的 callback_data(用 v1 sign,因为 enforce 调用 verify_button_token)
        token = await sign_button_token_with_nonce(
            principal_id=1001, action="delete_file",
            payload="ABC123",
        )

        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=int(__import__("time").time()) + 3600,
            nonce="nonce123", signature="a" * 32,
            mfa_verified=True,  # principal 已 MFA
            approver_id=2002,  # approver 与 principal 不同
            final_confirm=True,
            requires_mfa=True,
            requires_two_person=True,
            approver_mfa_verified=False,  # 关键:approver 未 MFA
        )

        with pytest.raises(AppError) as exc_info:
            await enforce_button_approval_policy(
                ctx, current_principal_id=1001,
                callback_data=token,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED
        params = exc_info.value.params
        assert params.get("reason") == "approver_mfa_required", (
            f"reason 应为 approver_mfa_required,实际: {params.get('reason')}"
        )

    @pytest.mark.asyncio
    async def test_two_person_passes_with_approver_mfa(self, store, monkeypatch):
        """R64 P0-05: requires_two_person=True + approver_mfa_verified=True → 通过。"""
        from services.button_approval_policy import (
            ButtonApprovalContext, enforce_button_approval_policy,
        )
        from services.button_security import sign_button_token_with_nonce

        token = await sign_button_token_with_nonce(
            principal_id=1001, action="delete_file",
            payload="ABC123",
        )

        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=int(__import__("time").time()) + 3600,
            nonce="nonce123", signature="a" * 32,
            mfa_verified=True,
            approver_id=2002,
            final_confirm=True,
            requires_mfa=True,
            requires_two_person=True,
            approver_mfa_verified=True,  # approver 已 MFA
        )

        valid, action, _ = await enforce_button_approval_policy(
            ctx, current_principal_id=1001,
            callback_data=token,
        )
        assert valid is True
        assert action == "delete_file"

    @pytest.mark.asyncio
    async def test_nominal_dual_approval_still_requires_approver_mfa(self, store, monkeypatch):
        """R64 P0-05: 名义双人审批(approver_id != principal_id 但 approver 无 MFA)被拒绝。

        这是 R64 P0-05 的核心整改点:此前仅 approver_id != principal_id 即可,
        现在还要求 approver 独立完成 MFA。
        """
        from services.button_approval_policy import (
            ButtonApprovalContext, enforce_button_approval_policy,
        )
        from services.button_security import sign_button_token_with_nonce
        from services.error_codes import AppError, ErrorCodes

        token = await sign_button_token_with_nonce(
            principal_id=1001, action="delete_file",
            payload="ABC123",
        )

        # approver_id != principal_id(满足旧规则),但 approver_mfa_verified=False
        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=int(__import__("time").time()) + 3600,
            nonce="nonce123", signature="a" * 32,
            mfa_verified=True,
            approver_id=2002,  # 不同于 principal
            final_confirm=True,
            requires_mfa=True,
            requires_two_person=True,
            approver_mfa_verified=False,  # 名义审批 — 应被拒绝
        )

        with pytest.raises(AppError) as exc_info:
            await enforce_button_approval_policy(
                ctx, current_principal_id=1001,
                callback_data=token,
            )
        # R64 P0-05: 即使 approver_id != principal_id,无 approver MFA 仍被拒绝
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED
        assert exc_info.value.params.get("reason") == "approver_mfa_required"

    @pytest.mark.asyncio
    async def test_two_person_still_requires_approver_id_different(self, store, monkeypatch):
        """R64 P0-05: requires_two_person=True 时,approver_id == principal_id 仍被拒绝。"""
        from services.button_approval_policy import (
            ButtonApprovalContext, enforce_button_approval_policy,
        )
        from services.button_security import sign_button_token_with_nonce
        from services.error_codes import AppError, ErrorCodes

        token = await sign_button_token_with_nonce(
            principal_id=1001, action="delete_file",
            payload="ABC123",
        )

        ctx = ButtonApprovalContext(
            action="delete_file", principal_id=1001,
            resource="file:ABC", resource_version="v1",
            request_hash="abc", expiry_ts=int(__import__("time").time()) + 3600,
            nonce="nonce123", signature="a" * 32,
            mfa_verified=True,
            approver_id=1001,  # 与 principal 相同 — 应被拒绝
            final_confirm=True,
            requires_mfa=True,
            requires_two_person=True,
            approver_mfa_verified=True,
        )

        with pytest.raises(AppError) as exc_info:
            await enforce_button_approval_policy(
                ctx, current_principal_id=1001,
                callback_data=token,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED
        assert exc_info.value.params.get("reason") == "approver_must_differ_from_principal"


# ════════════════════════════════════════════════════════════════
# D. button_security v2 签名
# ════════════════════════════════════════════════════════════════


class TestV2TokenSigning:
    """R64 P0-05: v2 签名 token 含 sub_action / session_id / locale。"""

    @pytest.mark.asyncio
    async def test_v2_token_has_9_segments(self, store):
        """v2 token 为 9 段格式(含 sub_action / session_id / locale)。"""
        from services.button_security import sign_button_token_with_nonce_v2

        token = await sign_button_token_with_nonce_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        parts = token.split(":")
        assert len(parts) == 9, (
            f"v2 token 应为 9 段,实际 {len(parts)} 段: {token}"
        )
        # 验证字段位置: principal_id:action:sub_action:session_id:locale:payload:expire_ts:nonce:signature
        assert parts[0] == "1001"
        assert parts[1] == "report"
        assert parts[2] == "detach"
        assert parts[3] == "sess_001"
        assert parts[4] == "zh-CN"
        assert parts[5] == "ABC123"

    @pytest.mark.asyncio
    async def test_v2_verify_extracts_new_fields(self, store):
        """v2 verify 提取 sub_action / session_id / locale。"""
        from services.button_security import (
            sign_button_token_with_nonce_v2, verify_button_token_v2,
        )

        token = await sign_button_token_with_nonce_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        valid, action, payload, sub_action, session_id, locale = await verify_button_token_v2(
            token, current_user_id=1001, store=store,
        )
        assert valid is True
        assert action == "report"
        assert payload == "ABC123"
        assert sub_action == "detach"
        assert session_id == "sess_001"
        assert locale == "zh-CN"

    @pytest.mark.asyncio
    async def test_v2_verify_rejects_tampered_sub_action(self, store):
        """v2 token 中 sub_action 被篡改 → 签名不匹配 → 拒绝。"""
        from services.button_security import (
            sign_button_token_with_nonce_v2, verify_button_token_v2,
        )

        token = await sign_button_token_with_nonce_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        # 篡改 sub_action 段(第 3 段,index=2)
        parts = token.split(":")
        parts[2] = "block"  # 篡改 detach → block
        tampered = ":".join(parts)

        valid, *_ = await verify_button_token_v2(
            tampered, current_user_id=1001, store=store,
        )
        assert valid is False, "篡改 sub_action 后签名应不匹配"

    @pytest.mark.asyncio
    async def test_v2_verify_rejects_tampered_session_id(self, store):
        """v2 token 中 session_id 被篡改 → 签名不匹配 → 拒绝。"""
        from services.button_security import (
            sign_button_token_with_nonce_v2, verify_button_token_v2,
        )

        token = await sign_button_token_with_nonce_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        parts = token.split(":")
        parts[3] = "sess_002"  # 篡改 session_id
        tampered = ":".join(parts)

        valid, *_ = await verify_button_token_v2(
            tampered, current_user_id=1001, store=store,
        )
        assert valid is False, "篡改 session_id 后签名应不匹配"

    @pytest.mark.asyncio
    async def test_v2_verify_rejects_tampered_locale(self, store):
        """v2 token 中 locale 被篡改 → 签名不匹配 → 拒绝。"""
        from services.button_security import (
            sign_button_token_with_nonce_v2, verify_button_token_v2,
        )

        token = await sign_button_token_with_nonce_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        parts = token.split(":")
        parts[4] = "en-US"  # 篡改 locale
        tampered = ":".join(parts)

        valid, *_ = await verify_button_token_v2(
            tampered, current_user_id=1001, store=store,
        )
        assert valid is False, "篡改 locale 后签名应不匹配"

    @pytest.mark.asyncio
    async def test_v2_verify_rejects_replay(self, store):
        """v2 nonce 原子消费:同一 token 第二次 verify 失败。"""
        from services.button_security import (
            sign_button_token_with_nonce_v2, verify_button_token_v2,
        )

        token = await sign_button_token_with_nonce_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        # 第一次 verify 成功
        valid1, *_ = await verify_button_token_v2(
            token, current_user_id=1001, store=store,
        )
        assert valid1 is True
        # 第二次 verify 应失败(nonce 已消费)
        valid2, *_ = await verify_button_token_v2(
            token, current_user_id=1001, store=store,
        )
        assert valid2 is False, "v2 nonce 已消费,第二次 verify 应失败"

    @pytest.mark.asyncio
    async def test_v1_token_still_works(self, store):
        """R64 P0-05: v1 函数向后兼容(签名格式不变)。"""
        from services.button_security import (
            sign_button_token_with_nonce, verify_button_token,
        )

        token = await sign_button_token_with_nonce(
            principal_id=1001, action="report",
            payload="ABC123",
        )
        # v1 token 为 6 段
        assert len(token.split(":")) == 6

        valid, action, payload = await verify_button_token(
            token, current_user_id=1001, store=store,
        )
        assert valid is True
        assert action == "report"
        assert payload == "ABC123"


class TestV2HandleBinding:
    """R64 P0-05: v2 handle 模式绑定 sub_action / session_id / locale。"""

    @pytest.mark.asyncio
    async def test_v2_handle_sign_and_verify_success(self, store):
        """v2 handle 模式:签名 + 验证 + 全部绑定匹配 → 通过。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
            audience="admin_callback",
            resource_version="ABC123:v1",
        )
        valid, action, payload = await verify_button_token_by_handle_v2(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            expected_sub_action="detach",
            expected_session_id="sess_001",
            expected_locale="zh-CN",
            expected_resource_version="ABC123:v1",
            store=store,
        )
        assert valid is True
        assert action == "report"
        assert payload == "ABC123"

    @pytest.mark.asyncio
    async def test_v2_handle_sub_action_mismatch_raises(self, store):
        """v2 handle: sub_action 不匹配 → AppError。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle_v2(
                handle_id, current_user_id=1001,
                expected_action="report",
                expected_audience="admin_callback",
                expected_sub_action="block",  # 不匹配
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_HASH_MISMATCH
        params = exc_info.value.params
        assert params.get("reason") == "sub_action_mismatch"
        assert params.get("expected") == "block"
        assert params.get("actual") == "detach"

    @pytest.mark.asyncio
    async def test_v2_handle_session_id_mismatch_raises(self, store):
        """v2 handle: session_id 不匹配 → AppError(防跨会话重放)。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle_v2(
                handle_id, current_user_id=1001,
                expected_action="report",
                expected_audience="admin_callback",
                expected_session_id="sess_002",  # 不匹配
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_BINDING_MISSING
        params = exc_info.value.params
        assert params.get("reason") == "session_id_mismatch"
        assert params.get("expected") == "sess_002"
        assert params.get("actual") == "sess_001"

    @pytest.mark.asyncio
    async def test_v2_handle_locale_mismatch_raises(self, store):
        """v2 handle: locale 不匹配 → AppError(防 locale 切换后旧按钮可点击)。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle_v2(
                handle_id, current_user_id=1001,
                expected_action="report",
                expected_audience="admin_callback",
                expected_locale="en-US",  # 不匹配
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_BINDING_MISSING
        params = exc_info.value.params
        assert params.get("reason") == "locale_mismatch"
        assert params.get("expected") == "en-US"
        assert params.get("actual") == "zh-CN"

    @pytest.mark.asyncio
    async def test_v2_handle_none_expected_skips_check(self, store):
        """v2 handle: expected_sub_action=None 时不检查(向后兼容)。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        # 不传 expected_sub_action / expected_session_id / expected_locale
        valid, action, payload = await verify_button_token_by_handle_v2(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid is True
        assert action == "report"

    @pytest.mark.asyncio
    async def test_v2_handle_action_mismatch_still_enforced(self, store):
        """v2 handle: action 不匹配仍被强制(v1 行为保留)。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )
        from services.error_codes import AppError, ErrorCodes

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        with pytest.raises(AppError) as exc_info:
            await verify_button_token_by_handle_v2(
                handle_id, current_user_id=1001,
                expected_action="restore",  # 不匹配
                expected_audience="admin_callback",
                store=store,
            )
        assert exc_info.value.code == ErrorCodes.BUTTON_POLICY_HASH_MISMATCH
        assert exc_info.value.params.get("reason") == "action_mismatch"

    @pytest.mark.asyncio
    async def test_v2_handle_nonce_atomic_consumption(self, store):
        """v2 handle: nonce 原子消费,同一 handle 第二次 verify 失败。"""
        from services.button_security import (
            sign_button_token_with_handle_v2, verify_button_token_by_handle_v2,
        )

        handle_id = await sign_button_token_with_handle_v2(
            principal_id=1001, action="report",
            payload="ABC123",
            sub_action="detach", session_id="sess_001", locale="zh-CN",
        )
        # 第一次 verify 成功
        valid1, _, _ = await verify_button_token_by_handle_v2(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid1 is True
        # 第二次 verify 应失败(nonce 已消费)
        valid2, _, _ = await verify_button_token_by_handle_v2(
            handle_id, current_user_id=1001,
            expected_action="report",
            expected_audience="admin_callback",
            store=store,
        )
        assert valid2 is False, "nonce 已消费,第二次 verify 应失败"


# ════════════════════════════════════════════════════════════════
# E. 整体集成:HighRiskPolicy → ButtonApprovalContext 联动
# ════════════════════════════════════════════════════════════════


class TestHighRiskPolicyButtonApprovalIntegration:
    """R64 P0-05: HighRiskPolicy 与 ButtonApprovalContext 联动测试。"""

    def test_build_context_from_high_risk_policy(self):
        """从 HighRiskPolicy 构造 ButtonApprovalContext,字段正确传递。"""
        from services.high_risk_policy import (
            get_policy, requires_mfa, requires_two_person,
        )
        from services.button_approval_policy import ButtonApprovalContext
        import time

        action = "delete_file"
        policy = get_policy(action)
        assert policy is not None

        ctx = ButtonApprovalContext(
            action=action,
            principal_id=1001,
            resource="file:ABC",
            resource_version="v1",
            request_hash="abc",
            expiry_ts=int(time.time()) + 3600,
            nonce="nonce123",
            signature="a" * 32,
            mfa_verified=True,
            approver_id=2002,
            final_confirm=True,
            # 从 HighRiskPolicy 注入
            requires_mfa=requires_mfa(action),
            requires_two_person=requires_two_person(action),
            approver_mfa_verified=True,
        )
        # 验证 HighRiskPolicy 字段正确传递到 Context
        assert ctx.requires_mfa == policy.requires_mfa
        assert ctx.requires_two_person == policy.requires_two_person
        assert ctx.requires_mfa is True
        assert ctx.requires_two_person is True

    def test_all_destructive_actions_have_consistent_mfa_and_two_person(self):
        """R64 P0-05 一致性:HIGH_RISK_POLICY 中所有 action 的
        requires_mfa 与 requires_two_person 必须同时为 True。"""
        from services.high_risk_policy import HIGH_RISK_POLICY

        for action, rule in HIGH_RISK_POLICY.items():
            assert rule.requires_mfa is True, (
                f"action={action} requires_mfa 必须为 True"
            )
            assert rule.requires_two_person is True, (
                f"action={action} requires_two_person 必须为 True"
            )
