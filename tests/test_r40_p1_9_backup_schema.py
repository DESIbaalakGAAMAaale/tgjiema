"""R40 P1-9: Backup Schema 补齐 12 张新表测试。

问题:
    R40 引入 12 张新业务表(tasks/collections/collection_items/notifications/
    content_reports/audit_log/quota_reservations/rbac_roles/rbac_user_roles/
    approvals/maintenance_state/admin_access_log),但 BACKUP_SCHEMA 字典未补全,
    导致:
      1. db_backup 不会备份这 12 张表的数据(数据丢失风险)
      2. db_restore 不支持恢复这 12 张表
      3. validate_schema() 报告 missing_tables
      4. _CACHE_STORE_DDL_TABLES frozenset 缺失这 12 张,导致 source 校验失败

整改:
    1. TableSchema 增加 local_only 字段(标记不参与 CRDB 同步的本地表)
    2. BACKUP_SCHEMA 新增 12 张表条目,source="sqlite"
    3. maintenance_state/admin_access_log 标记 local_only=True(纯本地状态)
    4. _CACHE_STORE_DDL_TABLES frozenset 补齐 12 张新表
    5. validate_schema() 返回 is_valid=True

测试策略:
    - 直接导入 BACKUP_SCHEMA 和 _CACHE_STORE_DDL_TABLES 做断言
    - 不依赖真实数据库(纯 schema 校验)
    - 验证 local_only 字段默认值为 False(向后兼容)
    - 验证 validate_schema() 通过
"""
from __future__ import annotations

from dataclasses import fields

import pytest


# ════════════════════════════════════════════════════════════════
# 测试数据: 12 张新表预期定义
# ════════════════════════════════════════════════════════════════

# 12 张新表名(均 source="sqlite")
NEW_TABLES = [
    "tasks",
    "collections",
    "collection_items",
    "notifications",
    "content_reports",
    "audit_log",
    "quota_reservations",
    "rbac_roles",
    "rbac_user_roles",
    "approvals",
    "maintenance_state",
    "admin_access_log",
]

# 纯本地状态表(不参与 CRDB 同步,标记 local_only=True)
LOCAL_ONLY_TABLES = {"maintenance_state", "admin_access_log"}

# 各表的主键(用于校验 pk_columns)
EXPECTED_PK = {
    "tasks": ("id",),
    "collections": ("id",),
    "collection_items": ("id",),
    "notifications": ("id",),
    "content_reports": ("id",),
    "audit_log": ("id",),
    "quota_reservations": ("id",),
    "rbac_roles": ("id",),
    "rbac_user_roles": ("user_id",),
    "approvals": ("id",),
    "maintenance_state": ("id",),  # CHECK(id=1)
    "admin_access_log": ("id",),
}


# ════════════════════════════════════════════════════════════════
# 1. TableSchema dataclass 字段扩展
# ════════════════════════════════════════════════════════════════

class TestTableSchemaLocalOnlyField:
    """R40 P1-9: TableSchema 应新增 local_only 字段。"""

    def test_local_only_field_exists(self):
        """TableSchema dataclass 应有 local_only 字段。"""
        from services.backup_schema import TableSchema
        field_names = {f.name for f in fields(TableSchema)}
        assert "local_only" in field_names, \
            "TableSchema 必须新增 local_only 字段(标记不参与 CRDB 同步的本地表)"

    def test_local_only_default_false(self):
        """local_only 字段默认值应为 False(向后兼容现有表)。"""
        from services.backup_schema import TableSchema
        # 构造一个最小 TableSchema(只传必填字段)
        ts = TableSchema(name="test", pk_columns=("id",))
        assert ts.local_only is False, \
            "local_only 默认值应为 False,确保现有表不受影响"


# ════════════════════════════════════════════════════════════════
# 2. BACKUP_SCHEMA 包含 12 张新表
# ════════════════════════════════════════════════════════════════

class TestBackupSchemaContainsNewTables:
    """R40 P1-9: BACKUP_SCHEMA 应包含 12 张新表。"""

    def test_all_new_tables_in_schema(self):
        """BACKUP_SCHEMA 必须包含 12 张新表。"""
        from services.backup_schema import BACKUP_SCHEMA
        missing = [t for t in NEW_TABLES if t not in BACKUP_SCHEMA]
        assert not missing, \
            f"BACKUP_SCHEMA 缺少 {len(missing)} 张新表: {missing}"

    def test_new_tables_source_is_sqlite(self):
        """12 张新表的 source 应为 'sqlite'(SQLite-only)。"""
        from services.backup_schema import BACKUP_SCHEMA
        for table_name in NEW_TABLES:
            ts = BACKUP_SCHEMA[table_name]
            assert ts.source == "sqlite", \
                f"表 {table_name} source 应为 'sqlite',实际: {ts.source}"

    def test_new_tables_not_large(self):
        """12 张新表 is_large 应为 False(必须参与备份)。"""
        from services.backup_schema import BACKUP_SCHEMA
        for table_name in NEW_TABLES:
            ts = BACKUP_SCHEMA[table_name]
            assert ts.is_large is False, \
                f"表 {table_name} is_large 应为 False(必须备份),实际: {ts.is_large}"

    def test_new_tables_pk_columns_correct(self):
        """12 张新表 pk_columns 应与 DDL 主键一致。"""
        from services.backup_schema import BACKUP_SCHEMA
        for table_name, expected_pk in EXPECTED_PK.items():
            ts = BACKUP_SCHEMA[table_name]
            assert ts.pk_columns == expected_pk, \
                f"表 {table_name} pk_columns 应为 {expected_pk},实际: {ts.pk_columns}"

    def test_new_tables_columns_non_empty(self):
        """12 张新表 columns 应非空(必须有列定义)。"""
        from services.backup_schema import BACKUP_SCHEMA
        for table_name in NEW_TABLES:
            ts = BACKUP_SCHEMA[table_name]
            assert ts.columns, \
                f"表 {table_name} columns 不能为空(必须补全列定义)"
            # 每张表至少应有 3 个列(主键 + 业务列)
            assert len(ts.columns) >= 3, \
                f"表 {table_name} columns 至少 3 个,实际: {len(ts.columns)}"


# ════════════════════════════════════════════════════════════════
# 3. local_only 标记(maintenance_state / admin_access_log)
# ════════════════════════════════════════════════════════════════

class TestLocalOnlyMarking:
    """R40 P1-9: maintenance_state 和 admin_access_log 应标记 local_only=True。"""

    def test_maintenance_state_local_only(self):
        """maintenance_state 应标记 local_only=True(纯本地状态,不参与 CRDB)。"""
        from services.backup_schema import BACKUP_SCHEMA
        ts = BACKUP_SCHEMA["maintenance_state"]
        assert ts.local_only is True, \
            f"maintenance_state 应标记 local_only=True,实际: {ts.local_only}"

    def test_admin_access_log_local_only(self):
        """admin_access_log 应标记 local_only=True(本地审计,不参与 CRDB)。"""
        from services.backup_schema import BACKUP_SCHEMA
        ts = BACKUP_SCHEMA["admin_access_log"]
        assert ts.local_only is True, \
            f"admin_access_log 应标记 local_only=True,实际: {ts.local_only}"

    def test_audit_log_not_local_only(self):
        """audit_log 应标记 local_only=False(需跨机同步审计日志)。"""
        from services.backup_schema import BACKUP_SCHEMA
        ts = BACKUP_SCHEMA["audit_log"]
        assert ts.local_only is False, \
            f"audit_log 应保持 local_only=False(跨机审计),实际: {ts.local_only}"

    def test_tasks_not_local_only(self):
        """tasks 应标记 local_only=False(任务状态需跨机同步)。"""
        from services.backup_schema import BACKUP_SCHEMA
        ts = BACKUP_SCHEMA["tasks"]
        assert ts.local_only is False, \
            f"tasks 应保持 local_only=False(跨机任务同步),实际: {ts.local_only}"

    def test_local_only_only_for_two_tables(self):
        """仅 maintenance_state/admin_access_log 标记 local_only=True。"""
        from services.backup_schema import BACKUP_SCHEMA
        local_only_tables = {
            name for name, ts in BACKUP_SCHEMA.items() if ts.local_only
        }
        # 至少包含这两张表
        assert LOCAL_ONLY_TABLES.issubset(local_only_tables), \
            f"local_only 表至少应包含 {LOCAL_ONLY_TABLES},实际: {local_only_tables}"
        # 12 张新表里,仅这两张应为 local_only
        new_local_only = local_only_tables & set(NEW_TABLES)
        assert new_local_only == LOCAL_ONLY_TABLES, \
            f"12 张新表里仅 {LOCAL_ONLY_TABLES} 应为 local_only,实际: {new_local_only}"


# ════════════════════════════════════════════════════════════════
# 4. _CACHE_STORE_DDL_TABLES frozenset 补齐
# ════════════════════════════════════════════════════════════════

class TestCacheStoreDdlTablesFrozenset:
    """R40 P1-9: _CACHE_STORE_DDL_TABLES frozenset 应包含 12 张新表。"""

    def test_new_tables_in_cache_store_ddl_tables(self):
        """_CACHE_STORE_DDL_TABLES 必须包含 12 张新表。"""
        from services.backup_schema import _CACHE_STORE_DDL_TABLES
        missing = [t for t in NEW_TABLES if t not in _CACHE_STORE_DDL_TABLES]
        assert not missing, \
            f"_CACHE_STORE_DDL_TABLES 缺少 {len(missing)} 张表: {missing}"

    def test_cache_store_ddl_tables_is_frozenset(self):
        """_CACHE_STORE_DDL_TABLES 应为 frozenset(不可变)。"""
        from services.backup_schema import _CACHE_STORE_DDL_TABLES
        assert isinstance(_CACHE_STORE_DDL_TABLES, frozenset), \
            f"_CACHE_STORE_DDL_TABLES 应为 frozenset,实际: {type(_CACHE_STORE_DDL_TABLES)}"


# ════════════════════════════════════════════════════════════════
# 5. validate_schema() 一致性校验
# ════════════════════════════════════════════════════════════════

class TestValidateSchemaConsistency:
    """R40 P1-9: 12 张新表加入后不应在 validate_schema() 中报错。

    注:BACKUP_SCHEMA 中存在预存的多余表(kv_config)与缺失表
    (pending_notify/dsp_notify/local_job_queue 等),这些不属于 P1-9 范围,
    本测试仅校验 12 张新表本身不引发 source_mismatches 或 empty_columns。
    """

    def test_new_tables_not_in_missing(self):
        """12 张新表加入 BACKUP_SCHEMA 后,不应再出现在 missing_tables。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        for table in NEW_TABLES:
            assert table not in result["missing_tables"], \
                f"新表 {table} 不应缺失: {result['missing_tables']}"

    def test_new_tables_no_source_mismatch(self):
        """12 张新表 source 标记应与 DDL 一致(无 source_mismatches)。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        for table in NEW_TABLES:
            assert table not in result["source_mismatches"], \
                f"新表 {table} source 错配: {result['source_mismatches']}"

    def test_new_tables_no_empty_columns(self):
        """12 张新表 columns 应非空(不出现在 empty_columns)。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        for table in NEW_TABLES:
            assert table not in result["empty_columns"], \
                f"新表 {table} columns 为空: {result['empty_columns']}"

    def test_new_tables_not_in_extra(self):
        """12 张新表应同时存在于 DDL frozenset(不出现在 extra_tables)。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        for table in NEW_TABLES:
            assert table not in result["extra_tables"], \
                f"新表 {table} 不应在 DDL 中缺失(否则无法校验 source): {result['extra_tables']}"


# ════════════════════════════════════════════════════════════════
# 6. 备份/恢复列表包含新表
# ════════════════════════════════════════════════════════════════

class TestBackupRestoreIncludesNewTables:
    """R40 P1-9: get_backup_tables / get_restore_tables 应包含新表。"""

    def test_backup_tables_includes_new_tables(self):
        """get_backup_tables() 应包含 12 张新表(非大表,需备份)。"""
        from services.backup_schema import get_backup_tables
        backup_tables = set(get_backup_tables())
        missing = [t for t in NEW_TABLES if t not in backup_tables]
        assert not missing, \
            f"get_backup_tables 缺少 {len(missing)} 张表: {missing}"

    def test_restore_tables_includes_new_tables(self):
        """get_restore_tables() 应包含 12 张新表。"""
        from services.backup_schema import get_restore_tables
        restore_tables = set(get_restore_tables())
        missing = [t for t in NEW_TABLES if t not in restore_tables]
        assert not missing, \
            f"get_restore_tables 缺少 {len(missing)} 张表: {missing}"

    def test_get_tables_by_source_sqlite_includes_new(self):
        """get_tables_by_source('sqlite') 应包含 12 张新表。"""
        from services.backup_schema import get_tables_by_source
        sqlite_tables = set(get_tables_by_source("sqlite"))
        missing = [t for t in NEW_TABLES if t not in sqlite_tables]
        assert not missing, \
            f"get_tables_by_source('sqlite') 缺少 {len(missing)} 张表: {missing}"

    def test_restore_tables_by_source_sqlite_includes_new(self):
        """get_restore_tables_by_source('sqlite') 应包含 12 张新表。"""
        from services.backup_schema import get_restore_tables_by_source
        sqlite_restore = set(get_restore_tables_by_source("sqlite"))
        missing = [t for t in NEW_TABLES if t not in sqlite_restore]
        assert not missing, \
            f"get_restore_tables_by_source('sqlite') 缺少 {len(missing)} 张表: {missing}"


# ════════════════════════════════════════════════════════════════
# 7. 列定义校验(关键列不缺失)
# ════════════════════════════════════════════════════════════════

class TestNewTablesColumnDefinitions:
    """R40 P1-9: 12 张新表 columns 应与 cache_store.py DDL 一致。"""

    def test_tasks_columns(self):
        """tasks 表应包含 12 个列(与 cache_store DDL 一致)。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["tasks"].columns
        expected = {
            "id", "task_type", "user_id", "status", "progress",
            "eta_seconds", "payload", "result", "error", "trace_id",
            "created_at", "updated_at",
        }
        assert set(cols) == expected, \
            f"tasks 列定义不符,实际: {set(cols)}"

    def test_collections_columns(self):
        """collections 表应包含 9 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["collections"].columns
        expected = {
            "id", "name", "code", "owner_id", "description",
            "version", "item_count", "status", "created_at", "updated_at",
        }
        assert set(cols) == expected, \
            f"collections 列定义不符,实际: {set(cols)}"

    def test_collection_items_columns(self):
        """collection_items 表应包含 4 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["collection_items"].columns
        expected = {"id", "collection_id", "file_code", "added_at"}
        assert set(cols) == expected, \
            f"collection_items 列定义不符,实际: {set(cols)}"

    def test_notifications_columns(self):
        """notifications 表应包含 7 个列(含 read_at)。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["notifications"].columns
        expected = {
            "id", "user_id", "type", "payload", "is_read",
            "created_at", "read_at",
        }
        assert set(cols) == expected, \
            f"notifications 列定义不符,实际: {set(cols)}"

    def test_content_reports_columns(self):
        """content_reports 表应包含 11 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["content_reports"].columns
        expected = {
            "id", "reporter_id", "target_type", "target_id", "reason",
            "description", "status", "appeal_text", "appealed_at",
            "resolved_by", "resolved_at", "created_at",
        }
        assert set(cols) == expected, \
            f"content_reports 列定义不符,实际: {set(cols)}"

    def test_audit_log_columns(self):
        """audit_log 表应包含 8 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["audit_log"].columns
        expected = {
            "id", "actor_id", "actor_type", "action", "target_type",
            "target_id", "details", "ip_addr", "created_at",
        }
        assert set(cols) == expected, \
            f"audit_log 列定义不符,实际: {set(cols)}"

    def test_quota_reservations_columns(self):
        """quota_reservations 表应包含 9 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["quota_reservations"].columns
        expected = {
            "id", "user_id", "amount", "reason", "status",
            "actual_amount", "created_at", "settled_at", "expired_at",
        }
        assert set(cols) == expected, \
            f"quota_reservations 列定义不符,实际: {set(cols)}"

    def test_rbac_roles_columns(self):
        """rbac_roles 表应包含 5 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["rbac_roles"].columns
        expected = {"id", "name", "description", "permissions", "created_at"}
        assert set(cols) == expected, \
            f"rbac_roles 列定义不符,实际: {set(cols)}"

    def test_rbac_user_roles_columns(self):
        """rbac_user_roles 表应包含 4 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["rbac_user_roles"].columns
        expected = {"user_id", "role_id", "assigned_at", "assigned_by"}
        assert set(cols) == expected, \
            f"rbac_user_roles 列定义不符,实际: {set(cols)}"

    def test_approvals_columns(self):
        """approvals 表应包含 9 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["approvals"].columns
        expected = {
            "id", "action", "payload", "status", "approver_id",
            "approver_note", "created_by", "created_at", "resolved_at",
        }
        assert set(cols) == expected, \
            f"approvals 列定义不符,实际: {set(cols)}"

    def test_maintenance_state_columns(self):
        """maintenance_state 表应包含 6 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["maintenance_state"].columns
        expected = {
            "id", "enabled", "reason", "started_by",
            "started_at", "ended_at",
        }
        assert set(cols) == expected, \
            f"maintenance_state 列定义不符,实际: {set(cols)}"

    def test_admin_access_log_columns(self):
        """admin_access_log 表应包含 8 个列。"""
        from services.backup_schema import BACKUP_SCHEMA
        cols = BACKUP_SCHEMA["admin_access_log"].columns
        expected = {
            "id", "admin_id", "action", "target_type", "target_id",
            "details", "ip_addr", "created_at",
        }
        assert set(cols) == expected, \
            f"admin_access_log 列定义不符,实际: {set(cols)}"


# ════════════════════════════════════════════════════════════════
# 8. 恢复依赖顺序(rbac_roles 在 rbac_user_roles 之前)
# ════════════════════════════════════════════════════════════════

class TestRestoreDependencyOrder:
    """R40 P1-9: 恢复时 rbac_roles 应在 rbac_user_roles 之前(外键依赖)。"""

    def test_rbac_roles_before_rbac_user_roles_in_restore_order(self):
        """get_restore_tables() 中 rbac_roles 应在 rbac_user_roles 之前。"""
        from services.backup_schema import get_restore_tables
        restore_tables = get_restore_tables()
        roles_idx = restore_tables.index("rbac_roles")
        user_roles_idx = restore_tables.index("rbac_user_roles")
        assert roles_idx < user_roles_idx, \
            f"rbac_roles (idx={roles_idx}) 应在 rbac_user_roles (idx={user_roles_idx}) 之前"

    def test_collections_before_collection_items(self):
        """get_restore_tables() 中 collections 应在 collection_items 之前。"""
        from services.backup_schema import get_restore_tables
        restore_tables = get_restore_tables()
        coll_idx = restore_tables.index("collections")
        items_idx = restore_tables.index("collection_items")
        assert coll_idx < items_idx, \
            f"collections (idx={coll_idx}) 应在 collection_items (idx={items_idx}) 之前"
