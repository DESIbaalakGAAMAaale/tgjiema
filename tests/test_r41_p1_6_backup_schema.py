"""R41 P1-6: Backup Schema 表 policy + 恢复顺序测试。

测试覆盖:
- TableSchema 新增字段默认值(backup_order=100, restore_policy="truncate_and_insert")
- 6 张新表(mfa_secrets/sessions/command_outbox/command_executions/dlq_records/ban_state)存在且字段正确
- backup_order 排序正确(父表 < 子表: collections<collection_items, rbac_roles<rbac_user_roles,
  command_outbox<command_executions)
- get_restore_order() 返回按 backup_order 排序的列表
- get_restore_policy() / get_skip_restore_tables() / get_backup_order() 函数行为正确
- local_only=True 的表(maintenance_state/sessions)在 include_local_only=False 时被排除
- restore_policy="skip" 的表(maintenance_state/sessions)在 get_skip_restore_tables() 中返回
- validate_schema() 通过(无 missing/extra/empty_columns)
"""
from __future__ import annotations

import pytest

from services.backup_schema import (
    BACKUP_SCHEMA,
    RESTORE_POLICY_SKIP,
    RESTORE_POLICY_TRUNCATE_AND_INSERT,
    RESTORE_POLICY_INSERT_IF_NOT_EXISTS,
    TableSchema,
    get_backup_order,
    get_restore_order,
    get_restore_policy,
    get_skip_restore_tables,
    validate_schema,
)


# ════════════════════════════════════════════════════════════════
# 1. TableSchema 新增字段默认值
# ════════════════════════════════════════════════════════════════

class TestTableSchemaDefaults:
    """R41 P1-6: TableSchema 新增 backup_order / restore_policy 字段默认值。"""

    def test_backup_order_default_is_100(self):
        """未指定 backup_order 时,默认值应为 100。"""
        ts = TableSchema(name="test_t", pk_columns=("id",))
        assert ts.backup_order == 100, "backup_order 默认值应为 100"

    def test_restore_policy_default_is_truncate_and_insert(self):
        """未指定 restore_policy 时,默认值应为 truncate_and_insert。"""
        ts = TableSchema(name="test_t", pk_columns=("id",))
        assert ts.restore_policy == "truncate_and_insert", \
            "restore_policy 默认值应为 truncate_and_insert"

    def test_local_only_default_is_false(self):
        """未指定 local_only 时,默认值应为 False(R40 P1-9 已有字段,R41 验证兼容)。"""
        ts = TableSchema(name="test_t", pk_columns=("id",))
        assert ts.local_only is False

    def test_backup_order_can_be_overridden(self):
        """显式指定 backup_order 应覆盖默认值。"""
        ts = TableSchema(name="test_t", pk_columns=("id",), backup_order=10)
        assert ts.backup_order == 10

    def test_restore_policy_can_be_overridden(self):
        """显式指定 restore_policy 应覆盖默认值。"""
        ts = TableSchema(
            name="test_t", pk_columns=("id",),
            restore_policy=RESTORE_POLICY_SKIP,
        )
        assert ts.restore_policy == "skip"

    def test_table_schema_is_frozen(self):
        """TableSchema 应为 frozen dataclass(不可变,防止运行时篡改)。"""
        ts = TableSchema(name="test_t", pk_columns=("id",))
        with pytest.raises((AttributeError, Exception)):
            # frozen dataclass 赋值应抛 FrozenInstanceError(继承 AttributeError)
            ts.backup_order = 999


# ════════════════════════════════════════════════════════════════
# 2. R41 P1-6 新增 6 张表存在性 + 字段校验
# ════════════════════════════════════════════════════════════════

# R41 P1-6 新增的 6 张表及其期望的关键字段
_R41_NEW_TABLES: dict[str, set[str]] = {
    "mfa_secrets": {"user_id", "secret", "enabled", "backup_codes",
                    "created_at", "updated_at"},
    "sessions": {"session_id", "user_id", "principal_id", "username",
                 "created_at", "expires_at", "last_activity_at",
                 "ip_addr", "user_agent"},
    "command_outbox": {"id", "command_type", "target_type", "target_id",
                       "payload", "status", "priority", "created_by",
                       "created_at", "processed_at", "attempts", "last_error"},
    "command_executions": {"id", "outbox_id", "executor", "status",
                           "started_at", "finished_at", "result", "error",
                           "attempts"},
    "dlq_records": {"id", "source", "original_payload", "error",
                    "failed_at", "attempts", "last_attempt_at",
                    "resolved", "resolved_at", "resolved_by"},
    "ban_state": {"user_id", "is_banned", "banned_at", "banned_by",
                  "reason", "expires_at", "unbanned_at", "unbanned_by"},
}


class TestR41NewTables:
    """R41 P1-6: 验证 6 张新表在 BACKUP_SCHEMA 中存在且字段完整。"""

    @pytest.mark.parametrize("table_name", list(_R41_NEW_TABLES.keys()))
    def test_table_exists_in_schema(self, table_name):
        """每张新表都应在 BACKUP_SCHEMA 中存在。"""
        assert table_name in BACKUP_SCHEMA, \
            f"R41 P1-6 新表 {table_name} 不在 BACKUP_SCHEMA 中"

    @pytest.mark.parametrize("table_name,expected_cols", list(_R41_NEW_TABLES.items()))
    def test_table_columns_complete(self, table_name, expected_cols):
        """每张新表的列定义应包含所有期望字段。"""
        if table_name not in BACKUP_SCHEMA:
            pytest.skip(f"{table_name} 不在 BACKUP_SCHEMA 中")
        actual_cols = set(BACKUP_SCHEMA[table_name].columns)
        missing = expected_cols - actual_cols
        assert not missing, \
            f"{table_name} 缺少列: {sorted(missing)} (实际: {sorted(actual_cols)})"

    @pytest.mark.parametrize("table_name", list(_R41_NEW_TABLES.keys()))
    def test_table_has_pk(self, table_name):
        """每张新表都应定义主键(非空)。"""
        if table_name not in BACKUP_SCHEMA:
            pytest.skip(f"{table_name} 不在 BACKUP_SCHEMA 中")
        pk = BACKUP_SCHEMA[table_name].pk_columns
        assert pk, f"{table_name} 主键为空"
        assert all(pk), f"{table_name} 主键含空字符串: {pk}"

    @pytest.mark.parametrize("table_name", list(_R41_NEW_TABLES.keys()))
    def test_table_source_is_sqlite(self, table_name):
        """6 张新表都应标记为 source='sqlite'(SQLite-only)。"""
        if table_name not in BACKUP_SCHEMA:
            pytest.skip(f"{table_name} 不在 BACKUP_SCHEMA 中")
        assert BACKUP_SCHEMA[table_name].source == "sqlite", \
            f"{table_name} source 应为 sqlite,实际: {BACKUP_SCHEMA[table_name].source}"

    @pytest.mark.parametrize("table_name", list(_R41_NEW_TABLES.keys()))
    def test_table_not_large(self, table_name):
        """6 张新表都应 is_large=False(需备份)。"""
        if table_name not in BACKUP_SCHEMA:
            pytest.skip(f"{table_name} 不在 BACKUP_SCHEMA 中")
        assert BACKUP_SCHEMA[table_name].is_large is False, \
            f"{table_name} 不应为大表"


# ════════════════════════════════════════════════════════════════
# 3. backup_order 外键依赖排序正确性
# ════════════════════════════════════════════════════════════════

class TestBackupOrderDependency:
    """R41 P1-6: 验证父表 backup_order < 子表 backup_order(外键依赖)。"""

    def test_collections_before_collection_items(self):
        """collections(父,order=10) < collection_items(子,order=11)。"""
        assert BACKUP_SCHEMA["collections"].backup_order == 10
        assert BACKUP_SCHEMA["collection_items"].backup_order == 11
        assert BACKUP_SCHEMA["collections"].backup_order < \
               BACKUP_SCHEMA["collection_items"].backup_order

    def test_rbac_roles_before_rbac_user_roles(self):
        """rbac_roles(父,order=10) < rbac_user_roles(子,order=11)。"""
        assert BACKUP_SCHEMA["rbac_roles"].backup_order == 10
        assert BACKUP_SCHEMA["rbac_user_roles"].backup_order == 11
        assert BACKUP_SCHEMA["rbac_roles"].backup_order < \
               BACKUP_SCHEMA["rbac_user_roles"].backup_order

    def test_command_outbox_before_command_executions(self):
        """command_outbox(父,order=50) < command_executions(子,order=51)。"""
        assert BACKUP_SCHEMA["command_outbox"].backup_order == 50
        assert BACKUP_SCHEMA["command_executions"].backup_order == 51
        assert BACKUP_SCHEMA["command_outbox"].backup_order < \
               BACKUP_SCHEMA["command_executions"].backup_order

    def test_mfa_secrets_order_is_80(self):
        """mfa_secrets 在用户相关表后恢复(backup_order=80)。"""
        assert BACKUP_SCHEMA["mfa_secrets"].backup_order == 80

    def test_ban_state_order_is_70(self):
        """ban_state 在命令/死信之后恢复(backup_order=70)。"""
        assert BACKUP_SCHEMA["ban_state"].backup_order == 70

    def test_dlq_records_order_is_60(self):
        """dlq_records 在命令执行后恢复(backup_order=60)。"""
        assert BACKUP_SCHEMA["dlq_records"].backup_order == 60

    def test_maintenance_state_order_is_last(self):
        """maintenance_state 本地状态最后恢复(backup_order=200)。"""
        assert BACKUP_SCHEMA["maintenance_state"].backup_order == 200

    def test_sessions_order_is_after_maintenance(self):
        """sessions 本地状态最后恢复(backup_order=210,> maintenance_state 的 200)。"""
        assert BACKUP_SCHEMA["sessions"].backup_order == 210
        assert BACKUP_SCHEMA["sessions"].backup_order > \
               BACKUP_SCHEMA["maintenance_state"].backup_order

    def test_admin_access_log_order_is_201(self):
        """admin_access_log 本地审计日志最后恢复(backup_order=201)。"""
        assert BACKUP_SCHEMA["admin_access_log"].backup_order == 201


# ════════════════════════════════════════════════════════════════
# 4. get_restore_order() 排序验证
# ════════════════════════════════════════════════════════════════

class TestGetRestoreOrder:
    """R41 P1-6: get_restore_order() 返回按 backup_order 排序的列表。"""

    def test_returns_list_of_table_names(self):
        """get_restore_order() 应返回 list[str]。"""
        order = get_restore_order()
        assert isinstance(order, list)
        assert all(isinstance(n, str) for n in order)

    def test_includes_all_tables_by_default(self):
        """默认 include_local_only=True 应包含所有 BACKUP_SCHEMA 表。"""
        order = get_restore_order()
        assert len(order) == len(BACKUP_SCHEMA), \
            f"默认应包含所有 {len(BACKUP_SCHEMA)} 张表,实际 {len(order)}"
        for name in BACKUP_SCHEMA:
            assert name in order, f"表 {name} 未在 restore_order 中"

    def test_sorted_by_backup_order(self):
        """恢复顺序应按 backup_order 升序排列。"""
        order = get_restore_order()
        orders = [BACKUP_SCHEMA[name].backup_order for name in order]
        # 验证升序(允许相同 order,按 name 字典序)
        for i in range(len(orders) - 1):
            assert orders[i] <= orders[i + 1], \
                f"位置 {i}: {order[i]}(order={orders[i]}) > {order[i+1]}(order={orders[i+1]})"

    def test_collections_before_collection_items_in_order(self):
        """恢复顺序中 collections 应在 collection_items 之前。"""
        order = get_restore_order()
        idx_coll = order.index("collections")
        idx_items = order.index("collection_items")
        assert idx_coll < idx_items, \
            f"collections 应在 collection_items 之前(位置 {idx_coll} vs {idx_items})"

    def test_rbac_roles_before_rbac_user_roles_in_order(self):
        """恢复顺序中 rbac_roles 应在 rbac_user_roles 之前。"""
        order = get_restore_order()
        idx_roles = order.index("rbac_roles")
        idx_user_roles = order.index("rbac_user_roles")
        assert idx_roles < idx_user_roles

    def test_command_outbox_before_command_executions_in_order(self):
        """恢复顺序中 command_outbox 应在 command_executions 之前。"""
        order = get_restore_order()
        idx_outbox = order.index("command_outbox")
        idx_exec = order.index("command_executions")
        assert idx_outbox < idx_exec

    def test_exclude_local_only_when_flag_false(self):
        """include_local_only=False 应排除 local_only=True 的表。"""
        order = get_restore_order(include_local_only=False)
        # 收集所有 local_only=True 的表名
        local_only_tables = [
            name for name, ts in BACKUP_SCHEMA.items() if ts.local_only
        ]
        for name in local_only_tables:
            assert name not in order, \
                f"local_only 表 {name} 应被排除(include_local_only=False)"

    def test_include_local_only_when_flag_true(self):
        """include_local_only=True(默认)应包含 local_only=True 的表。"""
        order = get_restore_order(include_local_only=True)
        local_only_tables = [
            name for name, ts in BACKUP_SCHEMA.items() if ts.local_only
        ]
        for name in local_only_tables:
            assert name in order, \
                f"local_only 表 {name} 应被包含(include_local_only=True)"

    def test_exclude_maintenance_state_when_flag_false(self):
        """maintenance_state(local_only=True)在 include_local_only=False 时应被排除。"""
        order = get_restore_order(include_local_only=False)
        assert "maintenance_state" not in order

    def test_exclude_sessions_when_flag_false(self):
        """sessions(local_only=True)在 include_local_only=False 时应被排除。"""
        order = get_restore_order(include_local_only=False)
        assert "sessions" not in order

    def test_stable_order_for_same_backup_order(self):
        """相同 backup_order 的表应按 name 字典序排列(稳定排序)。"""
        # 收集所有 backup_order 相同的表组
        from collections import defaultdict
        order_to_names: dict[int, list[str]] = defaultdict(list)
        for name, ts in BACKUP_SCHEMA.items():
            order_to_names[ts.backup_order].append(name)
        # 对每组按 name 排序,验证 restore_order 中同组也按 name 字典序
        full_order = get_restore_order()
        for order_val, names in order_to_names.items():
            if len(names) < 2:
                continue
            sorted_names = sorted(names)
            indices = [full_order.index(n) for n in sorted_names]
            # 验证 indices 升序(即 sorted_names 的顺序与 full_order 一致)
            for i in range(len(indices) - 1):
                assert indices[i] < indices[i + 1], \
                    f"backup_order={order_val} 组内排序不稳定: " \
                    f"{sorted_names[i]}(idx={indices[i]}) 应在 " \
                    f"{sorted_names[i+1]}(idx={indices[i+1]}) 之前"


# ════════════════════════════════════════════════════════════════
# 5. get_restore_policy() 函数行为
# ════════════════════════════════════════════════════════════════

class TestGetRestorePolicy:
    """R41 P1-6: get_restore_policy() 返回正确的恢复策略。"""

    def test_returns_truncate_and_insert_for_default_tables(self):
        """默认表(users/file_records 等)应返回 truncate_and_insert。"""
        # users 未显式指定 restore_policy,应使用默认值
        assert get_restore_policy("users") == RESTORE_POLICY_TRUNCATE_AND_INSERT
        assert get_restore_policy("file_records") == RESTORE_POLICY_TRUNCATE_AND_INSERT

    def test_returns_skip_for_maintenance_state(self):
        """maintenance_state 应返回 skip(R41 P1-6 不恢复维护状态)。"""
        assert get_restore_policy("maintenance_state") == RESTORE_POLICY_SKIP

    def test_returns_skip_for_sessions(self):
        """sessions 应返回 skip(R41 P1-6 不恢复过期 session)。"""
        assert get_restore_policy("sessions") == RESTORE_POLICY_SKIP

    def test_returns_insert_if_not_exists_for_admin_access_log(self):
        """admin_access_log 应返回 insert_if_not_exists(追加式恢复)。"""
        assert get_restore_policy("admin_access_log") == RESTORE_POLICY_INSERT_IF_NOT_EXISTS

    def test_returns_insert_if_not_exists_for_command_outbox(self):
        """command_outbox 应返回 insert_if_not_exists(不覆盖进行中的命令)。"""
        assert get_restore_policy("command_outbox") == RESTORE_POLICY_INSERT_IF_NOT_EXISTS

    def test_returns_insert_if_not_exists_for_command_executions(self):
        """command_executions 应返回 insert_if_not_exists。"""
        assert get_restore_policy("command_executions") == RESTORE_POLICY_INSERT_IF_NOT_EXISTS

    def test_returns_insert_if_not_exists_for_dlq_records(self):
        """dlq_records 应返回 insert_if_not_exists(追加式恢复)。"""
        assert get_restore_policy("dlq_records") == RESTORE_POLICY_INSERT_IF_NOT_EXISTS

    def test_returns_truncate_and_insert_for_mfa_secrets(self):
        """mfa_secrets 应返回 truncate_and_insert(凭证以备份为准)。"""
        assert get_restore_policy("mfa_secrets") == RESTORE_POLICY_TRUNCATE_AND_INSERT

    def test_returns_truncate_and_insert_for_ban_state(self):
        """ban_state 应返回 truncate_and_insert(最新封禁状态以备份为准)。"""
        assert get_restore_policy("ban_state") == RESTORE_POLICY_TRUNCATE_AND_INSERT

    def test_raises_valueerror_for_unknown_table(self):
        """未知表名应抛 ValueError。"""
        with pytest.raises(ValueError, match="不在 BACKUP_SCHEMA"):
            get_restore_policy("nonexistent_table_xyz")

    def test_all_policies_are_valid(self):
        """所有表的 restore_policy 都应在 _VALID_RESTORE_POLICIES 集合中。"""
        from services.backup_schema import _VALID_RESTORE_POLICIES
        for name, ts in BACKUP_SCHEMA.items():
            assert ts.restore_policy in _VALID_RESTORE_POLICIES, \
                f"{name} 的 restore_policy={ts.restore_policy} 不在合法集合中"


# ════════════════════════════════════════════════════════════════
# 6. get_skip_restore_tables() 函数行为
# ════════════════════════════════════════════════════════════════

class TestGetSkipRestoreTables:
    """R41 P1-6: get_skip_restore_tables() 返回 restore_policy=skip 的表列表。"""

    def test_returns_list(self):
        """应返回 list。"""
        result = get_skip_restore_tables()
        assert isinstance(result, list)

    def test_includes_maintenance_state(self):
        """maintenance_state(restore_policy=skip)应在列表中。"""
        result = get_skip_restore_tables()
        assert "maintenance_state" in result

    def test_includes_sessions(self):
        """sessions(restore_policy=skip)应在列表中。"""
        result = get_skip_restore_tables()
        assert "sessions" in result

    def test_excludes_default_tables(self):
        """默认表(users/file_records 等,restore_policy=truncate_and_insert)不应在列表中。"""
        result = get_skip_restore_tables()
        assert "users" not in result
        assert "file_records" not in result

    def test_excludes_insert_if_not_exists_tables(self):
        """insert_if_not_exists 的表不应在 skip 列表中。"""
        result = get_skip_restore_tables()
        assert "admin_access_log" not in result
        assert "command_outbox" not in result
        assert "command_executions" not in result
        assert "dlq_records" not in result

    def test_only_skip_policy_tables_included(self):
        """列表中所有表都应 restore_policy=skip。"""
        result = get_skip_restore_tables()
        for name in result:
            assert BACKUP_SCHEMA[name].restore_policy == RESTORE_POLICY_SKIP, \
                f"{name} 不应在 skip 列表中(restore_policy={BACKUP_SCHEMA[name].restore_policy})"

    def test_all_skip_tables_are_local_only(self):
        """restore_policy=skip 的表都应 local_only=True(只有本地状态才跳过恢复)。"""
        result = get_skip_restore_tables()
        for name in result:
            assert BACKUP_SCHEMA[name].local_only is True, \
                f"{name} restore_policy=skip 但 local_only=False(非本地状态不应跳过恢复)"


# ════════════════════════════════════════════════════════════════
# 7. get_backup_order() 函数行为
# ════════════════════════════════════════════════════════════════

class TestGetBackupOrder:
    """R41 P1-6: get_backup_order() 返回表的恢复顺序数字。"""

    def test_returns_int(self):
        """应返回 int。"""
        assert isinstance(get_backup_order("users"), int)

    def test_returns_100_for_default_tables(self):
        """未显式指定的表应返回默认值 100。"""
        assert get_backup_order("users") == 100
        assert get_backup_order("file_records") == 100

    def test_returns_10_for_collections(self):
        """collections 应返回 10(父表先恢复)。"""
        assert get_backup_order("collections") == 10

    def test_returns_11_for_collection_items(self):
        """collection_items 应返回 11(子表后恢复)。"""
        assert get_backup_order("collection_items") == 11

    def test_returns_50_for_command_outbox(self):
        """command_outbox 应返回 50。"""
        assert get_backup_order("command_outbox") == 50

    def test_returns_51_for_command_executions(self):
        """command_executions 应返回 51。"""
        assert get_backup_order("command_executions") == 51

    def test_returns_200_for_maintenance_state(self):
        """maintenance_state 应返回 200(本地状态最后)。"""
        assert get_backup_order("maintenance_state") == 200

    def test_returns_210_for_sessions(self):
        """sessions 应返回 210(本地状态最后)。"""
        assert get_backup_order("sessions") == 210

    def test_raises_valueerror_for_unknown_table(self):
        """未知表名应抛 ValueError。"""
        with pytest.raises(ValueError, match="不在 BACKUP_SCHEMA"):
            get_backup_order("nonexistent_table_xyz")


# ════════════════════════════════════════════════════════════════
# 8. local_only 标记正确性
# ════════════════════════════════════════════════════════════════

class TestLocalOnlyMarking:
    """R41 P1-6: local_only 标记正确(纯本地状态表应标记为 True)。"""

    def test_maintenance_state_is_local_only(self):
        """maintenance_state 应 local_only=True(纯本地维护状态)。"""
        assert BACKUP_SCHEMA["maintenance_state"].local_only is True

    def test_sessions_is_local_only(self):
        """sessions 应 local_only=True(session 不可跨实例共享)。"""
        assert BACKUP_SCHEMA["sessions"].local_only is True

    def test_admin_access_log_is_local_only(self):
        """admin_access_log 应 local_only=True(本地访问日志)。"""
        assert BACKUP_SCHEMA["admin_access_log"].local_only is True

    def test_mfa_secrets_not_local_only(self):
        """mfa_secrets 应 local_only=False(跨实例同步用户凭证)。"""
        assert BACKUP_SCHEMA["mfa_secrets"].local_only is False

    def test_ban_state_not_local_only(self):
        """ban_state 应 local_only=False(跨实例封禁状态一致)。"""
        assert BACKUP_SCHEMA["ban_state"].local_only is False

    def test_command_outbox_not_local_only(self):
        """command_outbox 应 local_only=False(需备份未完成命令)。"""
        assert BACKUP_SCHEMA["command_outbox"].local_only is False

    def test_command_executions_not_local_only(self):
        """command_executions 应 local_only=False(关联 outbox 备份)。"""
        assert BACKUP_SCHEMA["command_executions"].local_only is False

    def test_dlq_records_not_local_only(self):
        """dlq_records 应 local_only=False(死信队列需备份审计)。"""
        assert BACKUP_SCHEMA["dlq_records"].local_only is False


# ════════════════════════════════════════════════════════════════
# 9. validate_schema() 一致性校验
# ════════════════════════════════════════════════════════════════

class TestValidateSchema:
    """R41 P1-6: validate_schema() 应通过(无 missing/extra/empty_columns)。"""

    def test_returns_dict_with_required_keys(self):
        """validate_schema() 返回的 dict 应包含所有必需字段。"""
        result = validate_schema()
        required_keys = {
            "is_valid", "missing_tables", "extra_tables",
            "source_mismatches", "empty_columns", "details",
        }
        assert required_keys.issubset(result.keys()), \
            f"validate_schema() 缺少字段: {required_keys - set(result.keys())}"

    def test_no_empty_columns(self):
        """所有表的 columns 不应为空(empty_columns 应为空列表)。"""
        result = validate_schema()
        assert result["empty_columns"] == [], \
            f"columns 为空的表: {result['empty_columns']}"

    def test_r41_new_tables_have_columns(self):
        """R41 6 张新表的 columns 都不应为空。"""
        for table_name in _R41_NEW_TABLES:
            if table_name not in BACKUP_SCHEMA:
                continue
            cols = BACKUP_SCHEMA[table_name].columns
            assert cols, f"{table_name} 的 columns 为空(应补全列定义)"

    def test_r41_new_tables_in_cache_store_ddl(self):
        """R41 6 张新表都应在 _CACHE_STORE_DDL_TABLES 集合中(用于 validate_schema)。"""
        from services.backup_schema import _CACHE_STORE_DDL_TABLES
        for table_name in _R41_NEW_TABLES:
            assert table_name in _CACHE_STORE_DDL_TABLES, \
                f"{table_name} 不在 _CACHE_STORE_DDL_TABLES 中(validate_schema 会误报 missing)"
