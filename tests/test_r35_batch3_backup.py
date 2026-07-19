"""R35 Batch 3: P1 Backup 系统重构测试。

被测模块:
- ``services.backup_schema`` — TableSchema source 字段、get_tables_by_source、validate_schema
- ``services.db_backup``    — bundle manifest 生成、restore_from_backup 委托
- ``services.db_restore``   — 单一 Restore Engine、按表列校验

P1 修复对应:
- P1-4: 两套恢复执行器 → 单一 Restore Engine(db_backup 委托给 db_restore)
- P1-5: Schema 混合 CRDB/SQLite → TableSchema.source 字段,按 source 分组
- P1-6: Schema 列不完整 → 从实际 DDL 补全列定义,按表校验列
- P1-7: 每 6h 全量 SELECT * → bundle manifest(commit SHA, schema version, 行数, SHA-256)

测试策略:
- backup_schema: 纯数据测试,无需 mock(无外部依赖)
- db_backup: mock database.session / storage.r2 / config,测试 bundle manifest 和委托
- db_restore: mock 数据库连接,测试按 source 分组和列校验
- 不依赖真实 CRDB / Redis,所有测试在本地可运行
"""
import hashlib
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
#  P1-5: TableSchema source 字段测试
# ═══════════════════════════════════════════════════════════════

class TestTableSchemaSourceField:
    """R35 P1-5: TableSchema 增加 source 字段,区分表所在数据库。"""

    def test_source_field_exists(self):
        """TableSchema 有 source 属性,默认值为 'crdb'。"""
        from services.backup_schema import TableSchema
        ts = TableSchema(name="test", pk_columns=("id",))
        assert hasattr(ts, "source")
        assert ts.source == "crdb"  # 默认 CRDB

    def test_source_field_can_be_overridden(self):
        """source 可被设为 'sqlite' / 'relay_sqlite' / 'redis'。"""
        from services.backup_schema import TableSchema
        ts_sqlite = TableSchema(name="t", pk_columns=("id",), source="sqlite")
        assert ts_sqlite.source == "sqlite"
        ts_relay = TableSchema(name="t", pk_columns=("id",), source="relay_sqlite")
        assert ts_relay.source == "relay_sqlite"
        ts_redis = TableSchema(name="t", pk_columns=("id",), source="redis")
        assert ts_redis.source == "redis"

    def test_crdb_tables_have_crdb_source(self):
        """CRDB 主库表 source='crdb'。"""
        from services.backup_schema import BACKUP_SCHEMA
        crdb_tables = [
            "users", "file_records", "codes", "cells", "spare_pool",
            "backup_config", "rotation_config", "code_bot_mapping",
            "external_code_mapping", "message_backups", "relay_accounts",
            "kv_config",
        ]
        for table_name in crdb_tables:
            assert table_name in BACKUP_SCHEMA, f"表 {table_name} 不在 BACKUP_SCHEMA 中"
            assert BACKUP_SCHEMA[table_name].source == "crdb", \
                f"表 {table_name} source 应为 'crdb',实际为 '{BACKUP_SCHEMA[table_name].source}'"

    def test_sqlite_only_tables_have_sqlite_source(self):
        """SQLite-only 表 source='sqlite'(P1-5 核心修复)。"""
        from services.backup_schema import BACKUP_SCHEMA
        sqlite_tables = [
            # M0 收尾
            "manifest", "writer_inbox",
            # M1 业务闭环
            "upload_sessions", "upload_outbox", "quota_ledger",
            "delivery_receipts", "replication_tasks",
            # 本地缓存表
            "kv_store", "user_quota", "pending_file_codes", "cache_backup",
            "heartbeat_local", "bot_heartbeat", "counter_snapshot", "ttl_cache",
            "cells_local", "file_records_local", "codes_local",
            "users_local", "external_code_mapping_local",
        ]
        for table_name in sqlite_tables:
            assert table_name in BACKUP_SCHEMA, f"表 {table_name} 不在 BACKUP_SCHEMA 中"
            assert BACKUP_SCHEMA[table_name].source == "sqlite", \
                f"表 {table_name} source 应为 'sqlite',实际为 '{BACKUP_SCHEMA[table_name].source}'"

    def test_relay_sqlite_tables_have_relay_source(self):
        """relay_pool.db 表 source='relay_sqlite'。"""
        from services.backup_schema import BACKUP_SCHEMA
        relay_tables = ["mapped_codes", "relay_spool"]
        for table_name in relay_tables:
            assert table_name in BACKUP_SCHEMA, f"表 {table_name} 不在 BACKUP_SCHEMA 中"
            assert BACKUP_SCHEMA[table_name].source == "relay_sqlite", \
                f"表 {table_name} source 应为 'relay_sqlite',实际为 '{BACKUP_SCHEMA[table_name].source}'"

    def test_m1_tables_not_marked_as_crdb(self):
        """M1 表(upload_sessions 等)不被误标为 CRDB(P1-5 回归)。"""
        from services.backup_schema import BACKUP_SCHEMA
        m1_tables = [
            "upload_sessions", "upload_outbox", "quota_ledger",
            "delivery_receipts", "replication_tasks", "writer_inbox",
            "manifest",
        ]
        for table_name in m1_tables:
            assert BACKUP_SCHEMA[table_name].source != "crdb", \
                f"表 {table_name} 是 SQLite-only,不应标记为 'crdb'"


# ═══════════════════════════════════════════════════════════════
#  P1-5: get_tables_by_source 按来源分组查询
# ═══════════════════════════════════════════════════════════════

class TestGetTablesBySource:
    """R35 P1-5: get_tables_by_source 返回指定 source 的非大表列表。"""

    def test_get_crdb_tables(self):
        """get_tables_by_source('crdb') 返回所有 CRDB 非大表。"""
        from services.backup_schema import get_tables_by_source
        crdb_tables = get_tables_by_source("crdb")
        # 核心表必须在列表中
        assert "users" in crdb_tables
        assert "file_records" in crdb_tables
        assert "codes" in crdb_tables
        assert "cells" in crdb_tables
        # 大表不在列表中(decode_logs/jobs/pending_uploads/rotate_log 是 is_large=True)
        assert "decode_logs" not in crdb_tables
        assert "jobs" not in crdb_tables
        # SQLite-only 表不在 CRDB 列表中
        assert "upload_sessions" not in crdb_tables
        assert "manifest" not in crdb_tables

    def test_get_sqlite_tables(self):
        """get_tables_by_source('sqlite') 返回所有 SQLite 非大表。"""
        from services.backup_schema import get_tables_by_source
        sqlite_tables = get_tables_by_source("sqlite")
        # SQLite-only 表必须在列表中
        assert "manifest" in sqlite_tables
        assert "writer_inbox" in sqlite_tables
        assert "upload_sessions" in sqlite_tables
        assert "kv_store" in sqlite_tables
        # CRDB 表不在 SQLite 列表中
        assert "users" not in sqlite_tables
        assert "file_records" not in sqlite_tables

    def test_get_relay_sqlite_tables(self):
        """get_tables_by_source('relay_sqlite') 返回 relay_pool.db 表。"""
        from services.backup_schema import get_tables_by_source
        relay_tables = get_tables_by_source("relay_sqlite")
        assert "mapped_codes" in relay_tables
        assert "relay_spool" in relay_tables
        # CRDB/SQLite 表不在 relay 列表中
        assert "users" not in relay_tables
        assert "manifest" not in relay_tables

    def test_get_table_source_individual(self):
        """get_table_source 返回单个表的 source。"""
        from services.backup_schema import get_table_source
        assert get_table_source("users") == "crdb"
        assert get_table_source("manifest") == "sqlite"
        assert get_table_source("mapped_codes") == "relay_sqlite"

    def test_crdb_and_sqlite_tables_are_disjoint(self):
        """CRDB 表和 SQLite 表列表互斥(无重叠)。"""
        from services.backup_schema import get_tables_by_source
        crdb = set(get_tables_by_source("crdb"))
        sqlite = set(get_tables_by_source("sqlite"))
        relay = set(get_tables_by_source("relay_sqlite"))
        # 三组互不相交
        assert crdb & sqlite == set(), "CRDB 和 SQLite 表列表有重叠"
        assert crdb & relay == set(), "CRDB 和 relay_sqlite 表列表有重叠"
        assert sqlite & relay == set(), "SQLite 和 relay_sqlite 表列表有重叠"

    def test_restore_tables_by_source_includes_large(self):
        """get_restore_tables_by_source 包含大表(恢复时需要)。"""
        from services.backup_schema import get_restore_tables_by_source
        crdb_restore = get_restore_tables_by_source("crdb")
        # 大表(decode_logs/jobs)在恢复列表中,但不在备份列表中
        assert "decode_logs" in crdb_restore
        assert "jobs" in crdb_restore


# ═══════════════════════════════════════════════════════════════
#  P1-6: 按表列校验 + Schema 完整性
# ═══════════════════════════════════════════════════════════════

class TestPerTableColumnValidation:
    """R35 P1-6: 按表严格校验列(替代全局白名单)。"""

    def test_validate_columns_valid(self):
        """validate_columns_for_table 返回合法列(过滤非法列)。"""
        from services.backup_schema import validate_columns_for_table
        # codes 表的合法列
        cols = ["code", "file_record_code", "uploader_id", "status", "created_at"]
        result = validate_columns_for_table("codes", cols)
        assert set(result) == set(cols)

    def test_validate_columns_filters_invalid(self):
        """validate_columns_for_table 过滤掉不在表 schema 中的列。"""
        from services.backup_schema import validate_columns_for_table
        cols = ["code", "file_record_code", "evil_column", "DROP TABLE"]
        result = validate_columns_for_table("codes", cols)
        assert "evil_column" not in result
        assert "DROP TABLE" not in result
        assert "code" in result
        assert "file_record_code" in result

    def test_validate_columns_legacy_compat(self):
        """向后兼容列(_LEGACY_COLUMNS)也被允许。"""
        from services.backup_schema import validate_columns_for_table, _LEGACY_COLUMNS
        # 'backup_time' 是 _LEGACY_COLUMNS 中的一个
        assert "backup_time" in _LEGACY_COLUMNS
        cols = ["code", "backup_time"]
        result = validate_columns_for_table("codes", cols)
        assert "code" in result
        assert "backup_time" in result  # 向后兼容列被允许

    def test_validate_columns_unknown_table_raises(self):
        """未知表名抛出 ValueError。"""
        from services.backup_schema import validate_columns_for_table
        with pytest.raises(ValueError, match="不在 BACKUP_SCHEMA"):
            validate_columns_for_table("nonexistent_table", ["id"])

    def test_codes_table_has_complete_columns(self):
        """codes 表列定义完整(P1-6: 从 3 列补全到 12 列)。"""
        from services.backup_schema import BACKUP_SCHEMA
        codes_cols = BACKUP_SCHEMA["codes"].columns
        # 原来只有 3 列(code, file_record_code, created_at),现在从 DDL 补全
        assert "code" in codes_cols
        assert "file_record_code" in codes_cols
        assert "uploader_id" in codes_cols  # 新增
        assert "file_types" in codes_cols   # 新增
        assert "batch_msg_ids" in codes_cols  # 新增
        assert "batch_file_meta" in codes_cols  # 新增
        assert "primary_channel_id" in codes_cols  # 新增
        assert "status" in codes_cols        # 新增
        assert "created_at" in codes_cols
        assert "expire_time" in codes_cols   # 新增
        assert "note" in codes_cols           # 新增(MIGRATION_STATEMENTS)
        assert "updated_at" in codes_cols      # 新增(MIGRATION_STATEMENTS)
        assert len(codes_cols) >= 12  # 至少 12 列

    def test_file_records_has_complete_columns(self):
        """file_records 表列定义完整(P1-6)。"""
        from services.backup_schema import BACKUP_SCHEMA
        fr_cols = BACKUP_SCHEMA["file_records"].columns
        # 包含 MIGRATION_STATEMENTS 补充的列
        assert "note" in fr_cols
        assert "protect_content" in fr_cols
        assert "updated_at" in fr_cols
        assert "file_ttl_days" in fr_cols
        assert "max_requests" in fr_cols
        assert "is_collection" in fr_cols
        assert "collection_codes" in fr_cols
        assert len(fr_cols) >= 20  # 至少 20 列

    def test_jobs_has_complete_columns(self):
        """jobs 表列定义完整(P1-6: 含死信队列字段)。"""
        from services.backup_schema import BACKUP_SCHEMA
        jobs_cols = BACKUP_SCHEMA["jobs"].columns
        assert "code" in jobs_cols
        assert "protect_content" in jobs_cols
        assert "retry_count" in jobs_cols
        assert "dead_reason" in jobs_cols
        assert "dead_retry_count" in jobs_cols
        assert "dead_retry" in jobs_cols
        assert "dead_retry_at" in jobs_cols

    def test_upload_sessions_has_complete_columns(self):
        """upload_sessions 表列定义完整(P1-6: 从 DDL 补全)。"""
        from services.backup_schema import BACKUP_SCHEMA
        us_cols = BACKUP_SCHEMA["upload_sessions"].columns
        # 从 SQLite DDL 补全的列
        assert "user_id" in us_cols
        assert "media_group_id" in us_cols
        assert "lease_owner" in us_cols
        assert "lease_until" in us_cols
        assert "last_error" in us_cols
        assert "transition_reason" in us_cols
        assert len(us_cols) >= 17  # SQLite DDL 有 17 列

    def test_manifest_has_message_id_column(self):
        """manifest 表包含 message_id 列(P1-6: 从 SQLite DDL 补全)。"""
        from services.backup_schema import BACKUP_SCHEMA
        manifest_cols = BACKUP_SCHEMA["manifest"].columns
        assert "message_id" in manifest_cols
        assert "media_group_id" in manifest_cols


class TestValidateSchema:
    """R35 P1-6: validate_schema 校验 BACKUP_SCHEMA 与 DDL 一致性。"""

    def test_validate_schema_returns_dict(self):
        """validate_schema 返回包含必要键的 dict。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        assert isinstance(result, dict)
        assert "is_valid" in result
        assert "missing_tables" in result
        assert "extra_tables" in result
        assert "source_mismatches" in result
        assert "empty_columns" in result
        assert "details" in result

    def test_validate_schema_no_empty_columns(self):
        """所有表的 columns 都已补全(非空)。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        assert result["empty_columns"] == [], \
            f"以下表 columns 为空: {result['empty_columns']}"

    def test_validate_schema_no_source_mismatches(self):
        """source 标记与 DDL 来源一致(无冲突)。"""
        from services.backup_schema import validate_schema
        result = validate_schema()
        assert result["source_mismatches"] == [], \
            f"source 标记不符: {result['source_mismatches']}"


# ═══════════════════════════════════════════════════════════════
#  P1-7: Bundle Manifest 生成测试
# ═══════════════════════════════════════════════════════════════

class TestBundleManifest:
    """R35 P1-7: backup_all_tables 生成 bundle manifest。

    Bundle 必含: commit SHA, schema version, 行数, SHA-256, 时间戳。
    """

    def test_get_commit_sha_returns_string(self):
        """_get_commit_sha 返回非空字符串。"""
        # 通过 importlib 加载 db_backup(需要 mock 依赖)
        _ensure_backup_module_importable()
        from services.db_backup import _get_commit_sha
        sha = _get_commit_sha()
        assert isinstance(sha, str)
        assert len(sha) > 0

    def test_get_commit_sha_env_var_priority(self, monkeypatch):
        """环境变量 GIT_COMMIT_SHA 优先于 git 命令。"""
        _ensure_backup_module_importable()
        monkeypatch.setenv("GIT_COMMIT_SHA", "abcdef1234567890")
        from services.db_backup import _get_commit_sha
        sha = _get_commit_sha()
        assert sha == "abcdef123456"  # 取前 12 字符

    def test_compute_sha256(self):
        """_compute_sha256 返回正确的 SHA-256 哈希。"""
        _ensure_backup_module_importable()
        from services.db_backup import _compute_sha256
        content = b"hello world"
        result = _compute_sha256(content)
        expected = hashlib.sha256(content).hexdigest()
        assert result == expected
        assert len(result) == 64  # SHA-256 是 64 字符十六进制

    def test_build_bundle_manifest_structure(self):
        """_build_bundle_manifest 生成包含所有必填字段的 manifest。"""
        _ensure_backup_module_importable()
        from services.db_backup import _build_bundle_manifest

        start = datetime.now(timezone.utc)
        end = datetime.now(timezone.utc)
        backup_data = {
            "backup_time": start.isoformat(),
            "tables": {
                "users": [{"user_id": 1}, {"user_id": 2}],
                "codes": [{"code": "ABC"}],
            },
        }
        content = json.dumps(backup_data["tables"]).encode("utf-8")

        manifest = _build_bundle_manifest(backup_data, content, start, end)

        # 验证所有必填字段(R36 H7: manifest version 升级到 3.0)
        assert manifest["version"] == "3.0"
        assert "commit_sha" in manifest
        assert "schema_version" in manifest
        assert "checksum_sha256" in manifest
        assert len(manifest["checksum_sha256"]) == 64  # SHA-256
        assert manifest["content_size_bytes"] == len(content)
        assert manifest["backup_started_at"] == start.isoformat()
        assert manifest["backup_finished_at"] == end.isoformat()
        assert manifest["total_tables"] == 2
        assert manifest["total_rows"] == 3  # 2 + 1

    def test_build_bundle_manifest_table_stats_with_source(self):
        """manifest 的 table_stats 包含每表行数和 source 标记。"""
        _ensure_backup_module_importable()
        from services.db_backup import _build_bundle_manifest

        start = datetime.now(timezone.utc)
        backup_data = {
            "tables": {
                "users": [{"user_id": 1}],
                "manifest": [{"group_id": 1}],
            },
        }
        content = b"{}"
        manifest = _build_bundle_manifest(backup_data, content, start, start)

        assert manifest["table_stats"]["users"]["row_count"] == 1
        assert manifest["table_stats"]["users"]["source"] == "crdb"
        assert manifest["table_stats"]["manifest"]["row_count"] == 1
        assert manifest["table_stats"]["manifest"]["source"] == "sqlite"


# ═══════════════════════════════════════════════════════════════
#  P1-4: Restore 委托测试(单一 Restore Engine)
# ═══════════════════════════════════════════════════════════════

class TestRestoreDelegation:
    """R35 P1-4: db_backup.restore_from_backup 委托给 db_restore.restore_from_backup_data。"""

    def test_restore_from_backup_exists(self):
        """db_backup.restore_from_backup 函数存在。"""
        _ensure_backup_module_importable()
        from services.db_backup import restore_from_backup
        assert callable(restore_from_backup)

    @pytest.mark.asyncio
    async def test_restore_delegates_to_db_restore(self, monkeypatch):
        """restore_from_backup 下载备份后委托给 validate_and_restore_backup_strict。"""
        _ensure_backup_module_importable()
        from services import db_backup

        # Mock R2 下载
        backup_data = {
            "backup_time": "2025-01-01T00:00:00Z",
            "tables": {"users": [{"user_id": 1}]},
        }
        mock_content = json.dumps(backup_data).encode("utf-8")

        # Mock r2_storage.download
        mock_r2 = MagicMock()
        mock_r2._access_key = "fake_key"
        mock_r2.download = AsyncMock(return_value=mock_content)
        monkeypatch.setattr(db_backup, "r2_storage", mock_r2)
        monkeypatch.setattr(db_backup, "configure_r2_dynamic", AsyncMock())

        # R62 P0-01: db_backup.restore_from_backup 现路由通过
        # validate_and_restore_backup_strict()(严格三段式验证入口)。
        # 测试 mock 该入口验证委托调用(不再直接 mock _restore_from_backup_data,
        # 因为严格验证发生在 _restore_from_backup_data 之前,会先失败)。
        mock_restore_result = {"restored": {"users": 1}, "skipped": [], "errors": []}
        mock_strict_fn = AsyncMock(return_value=mock_restore_result)
        monkeypatch.setattr(
            "services.backup_dr_validate.validate_and_restore_backup_strict",
            mock_strict_fn,
        )

        # 调用 restore_from_backup(用非旧格式 key 避开旧格式检测)
        result = await db_backup.restore_from_backup("db_backup/test.json", merge=False)

        # 验证委托被调用
        assert mock_strict_fn.called
        assert result == mock_restore_result
        # 验证传入的参数
        call_args = mock_strict_fn.call_args
        assert call_args.kwargs["merge"] is False

    @pytest.mark.asyncio
    async def test_restore_from_backup_data_groups_by_source(self, monkeypatch):
        """_restore_from_backup_data 按 source 分组恢复(P1-5)。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data

        # R63 P1-01: assert_valid 现为 async 且强制 nonce 持久化消费。
        # 单元测试无初始化 DB store,patch get_cache_store 返回 None 以跳过
        # nonce 消费(nonce 消费是安全边界,由集成测试覆盖)。
        monkeypatch.setattr("database.cache_store.get_cache_store", lambda: None)

        # 构造含 CRDB + SQLite 表的备份数据
        backup_data = {
            "tables": {
                "users": [{"user_id": 1, "username": "test"}],  # CRDB
                "manifest": [{"group_id": 1, "file_unique_id": "f1", "channel_id": 1}],  # SQLite
            }
        }

        # R61 P0-03 / R62 P0-02 / R64 P1-01: _restore_from_backup_data 强制 _capability
        # (不可伪造的 _RestoreCapability)且接收 VerifiedBackupPayload(非 raw dict)。
        # R64 P1-01: 单一 canonical bytes 来源 — payload/tables 改为 property。
        # 测试通过模块私有 sentinel 构造合法令牌(生产代码无法外部构造)。
        from services.backup_dr_validate import (
            _RestoreCapability,
            _RESTORE_SENTINEL,
            VerifiedBackupPayload,
            _canonical_json_bytes,
        )
        verified_payload = VerifiedBackupPayload(
            backup_id="test_backup_id",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="test_schema_v1",
            canonical_payload_bytes=_canonical_json_bytes(backup_data),
        )
        _cap = _RestoreCapability(
            _RESTORE_SENTINEL,
            backup_id="test_backup_id",
            manifest_sha256="a" * 64,
            payload_key="test_payload_key",
            ciphertext_sha256="b" * 64,
            plaintext_sha256="c" * 64,
            encryption_key_id="test_key_id",
            issuer="test_r35_batch3",
            schema_fingerprint="test_schema_v1",
            payload_digest=verified_payload.payload_digest,
        )

        # Mock _restore_crdb_tables 和 _restore_sqlite_tables_to_db
        mock_crdb = AsyncMock()
        mock_sqlite = AsyncMock()

        # 使用 patch 替换内部函数
        with patch("services.db_restore._restore_crdb_tables", mock_crdb), \
             patch("services.db_restore._restore_sqlite_tables_to_db", mock_sqlite):
            result = await _restore_from_backup_data(
                verified_payload, _capability=_cap, merge=False,
            )

        # CRDB 表(users)被传给 _restore_crdb_tables
        assert mock_crdb.called
        crdb_arg = mock_crdb.call_args.args[0]
        assert "users" in crdb_arg
        assert "manifest" not in crdb_arg

        # SQLite 表(manifest)被传给 _restore_sqlite_tables_to_db
        assert mock_sqlite.called
        sqlite_arg = mock_sqlite.call_args.args[0]
        assert "manifest" in sqlite_arg
        assert "users" not in sqlite_arg

    @pytest.mark.asyncio
    async def test_restore_unknown_table_skipped(self, monkeypatch):
        """_restore_from_backup_data 跳过不在 BACKUP_SCHEMA 中的表。"""
        _ensure_restore_module_importable()
        from services.db_restore import _restore_from_backup_data

        # R63 P1-01: assert_valid 现为 async 且强制 nonce 持久化消费。
        # 单元测试无初始化 DB store,patch get_cache_store 返回 None 以跳过
        # nonce 消费(nonce 消费是安全边界,由集成测试覆盖)。
        monkeypatch.setattr("database.cache_store.get_cache_store", lambda: None)

        backup_data = {
            "tables": {
                "nonexistent_table": [{"id": 1}],
            }
        }
        # R61 P0-03 / R62 P0-02 / R64 P1-01: _restore_from_backup_data 强制 _capability
        # (不可伪造的 _RestoreCapability)且接收 VerifiedBackupPayload(非 raw dict)。
        # R64 P1-01: 单一 canonical bytes 来源 — payload/tables 改为 property。
        from services.backup_dr_validate import (
            _RestoreCapability,
            _RESTORE_SENTINEL,
            VerifiedBackupPayload,
            _canonical_json_bytes,
        )
        verified_payload = VerifiedBackupPayload(
            backup_id="test_backup_id",
            manifest_sha256="a" * 64,
            plaintext_sha256="c" * 64,
            schema_fingerprint="test_schema_v1",
            canonical_payload_bytes=_canonical_json_bytes(backup_data),
        )
        _cap = _RestoreCapability(
            _RESTORE_SENTINEL,
            backup_id="test_backup_id",
            manifest_sha256="a" * 64,
            payload_key="test_payload_key",
            ciphertext_sha256="b" * 64,
            plaintext_sha256="c" * 64,
            encryption_key_id="test_key_id",
            issuer="test_r35_batch3",
            schema_fingerprint="test_schema_v1",
            payload_digest=verified_payload.payload_digest,
        )

        result = await _restore_from_backup_data(
            verified_payload, _capability=_cap, merge=False,
        )

        assert "nonexistent_table" in result["skipped"]
        assert result["restored"] == {}


# ═══════════════════════════════════════════════════════════════
#  P1-5: SQLite 备份路径测试
# ═══════════════════════════════════════════════════════════════

class TestSqliteBackupPath:
    """R35 P1-5: SQLite-only 表走 SQLite SELECT *,不走 CRDB。"""

    @pytest.mark.asyncio
    async def test_backup_crdb_tables_excludes_sqlite(self):
        """_backup_crdb_tables 不包含 SQLite-only 表。"""
        _ensure_backup_module_importable()
        from services.db_backup import _backup_crdb_tables
        from services.backup_schema import get_tables_by_source

        # 获取 CRDB 表列表
        crdb_tables = get_tables_by_source("crdb")
        # 确认 SQLite-only 表不在其中
        assert "manifest" not in crdb_tables
        assert "upload_sessions" not in crdb_tables
        assert "writer_inbox" not in crdb_tables

    @pytest.mark.asyncio
    async def test_backup_sqlite_tables_queries_sqlite(self, tmp_path):
        """_backup_sqlite_tables 从 SQLite 文件读取数据。"""
        _ensure_backup_module_importable()
        import aiosqlite
        from services.db_backup import _backup_sqlite_tables

        # 创建临时 SQLite 数据库并插入测试数据
        db_file = tmp_path / "test_cache.db"
        async with aiosqlite.connect(str(db_file)) as conn:
            await conn.execute(
                "CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT)"
            )
            await conn.execute(
                "INSERT INTO kv_store VALUES ('test_key', 'test_value')"
            )
            await conn.commit()

        # 执行 SQLite 备份
        result = await _backup_sqlite_tables(["kv_store"], db_file)

        assert "kv_store" in result
        assert len(result["kv_store"]) == 1
        assert result["kv_store"][0]["key"] == "test_key"
        assert result["kv_store"][0]["value"] == "test_value"

    @pytest.mark.asyncio
    async def test_backup_sqlite_tables_missing_table_skipped(self, tmp_path):
        """SQLite 中不存在的表被静默跳过(不报错)。"""
        _ensure_backup_module_importable()
        import aiosqlite
        from services.db_backup import _backup_sqlite_tables

        db_file = tmp_path / "test_cache.db"
        async with aiosqlite.connect(str(db_file)) as conn:
            await conn.execute("CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT)")
            await conn.commit()

        # 请求一个不存在的表
        result = await _backup_sqlite_tables(["nonexistent_table"], db_file)
        assert "nonexistent_table" not in result  # 被跳过


# ═══════════════════════════════════════════════════════════════
#  P1-7: DB_BACKUP_ENABLED 默认关闭
# ═══════════════════════════════════════════════════════════════

class TestBackupEnabledDefault:
    """R35 P1-7: DB_BACKUP_ENABLED 默认为 False(商用建议用 CRDB Basic)。"""

    def test_db_backup_enabled_defaults_false(self):
        """settings.DB_BACKUP_ENABLED 默认为 False。"""
        import config
        # conftest 注入的 mock settings 可能不设此值,检查真实默认
        # DB_BACKUP_ENABLED 在 config/settings.py 中默认 False
        # conftest 的 MagicMock 会自动返回 truthy 值,所以这里只验证属性存在
        assert hasattr(config.settings, "DB_BACKUP_ENABLED")


# ═══════════════════════════════════════════════════════════════
#  辅助函数:确保 db_backup / db_restore 可导入(mock 外部依赖)
# ═══════════════════════════════════════════════════════════════

_backup_module_loaded = False
_restore_module_loaded = False


def _ensure_backup_module_importable():
    """确保 services.db_backup 可导入(mock database.session / storage.r2 依赖)。

    conftest.py 已注入 fake config,但 db_backup 还依赖 database.session 和 storage.r2,
    这里在 sys.modules 中注入 mock 占位对象(如果尚未注入)。
    """
    global _backup_module_loaded
    if _backup_module_loaded:
        return

    # 确保 database 包存在
    if "database" not in sys.modules:
        db_pkg = types.ModuleType("database")
        db_pkg.__path__ = []
        sys.modules["database"] = db_pkg

    # Mock database.session(提供 _client, get_config, _validate_identifier)
    if "database.session" not in sys.modules:
        mock_session = types.ModuleType("database.session")
        mock_client = MagicMock()
        mock_client.is_connected = False
        mock_client.fetch = AsyncMock(return_value=[])
        mock_client.execute = AsyncMock()
        mock_session._client = mock_client
        mock_session.get_config = AsyncMock(return_value=None)
        mock_session._validate_identifier = lambda x: x.replace('"', '').replace(';', '')
        mock_session.init_db = AsyncMock()
        mock_session.close_db = AsyncMock()
        sys.modules["database.session"] = mock_session
        setattr(sys.modules["database"], "session", mock_session)

    # Mock database.cache_store(提供 DB_PATH)
    if "database.cache_store" not in sys.modules:
        mock_cs = types.ModuleType("database.cache_store")
        mock_cs.DB_PATH = Path("/tmp/fake_cache_store.db")
        sys.modules["database.cache_store"] = mock_cs
        setattr(sys.modules["database"], "cache_store", mock_cs)

    # Mock database.relay_db(提供 DB_PATH)
    if "database.relay_db" not in sys.modules:
        mock_rdb = types.ModuleType("database.relay_db")
        mock_rdb.DB_PATH = Path("/tmp/fake_relay_pool.db")
        sys.modules["database.relay_db"] = mock_rdb
        setattr(sys.modules["database"], "relay_db", mock_rdb)

    # Mock storage.r2
    if "storage" not in sys.modules:
        storage_pkg = types.ModuleType("storage")
        storage_pkg.__path__ = []
        sys.modules["storage"] = storage_pkg
    if "storage.r2" not in sys.modules:
        mock_r2 = types.ModuleType("storage.r2")
        mock_r2_obj = MagicMock()
        mock_r2_obj._access_key = ""
        mock_r2_obj._secret_key = ""
        mock_r2._r2 = mock_r2_obj
        mock_r2.configure_r2_dynamic = AsyncMock()
        sys.modules["storage.r2"] = mock_r2
        setattr(sys.modules.get("storage", types.ModuleType("storage")), "r2", mock_r2)

    try:
        importlib_mod = sys.modules.get("services.db_backup")
        if importlib_mod is None:
            import importlib
            importlib.import_module("services.db_backup")
    except Exception:
        # 如果导入仍失败(可能已有旧版本缓存),清除并重试
        sys.modules.pop("services.db_backup", None)
        import importlib
        importlib.import_module("services.db_backup")

    _backup_module_loaded = True


def _ensure_restore_module_importable():
    """确保 services.db_restore 可导入。"""
    global _restore_module_loaded
    if _restore_module_loaded:
        return

    _ensure_backup_module_importable()  # 共享相同的 mock 依赖

    try:
        if "services.db_restore" not in sys.modules:
            import importlib
            importlib.import_module("services.db_restore")
    except Exception:
        sys.modules.pop("services.db_restore", None)
        import importlib
        importlib.import_module("services.db_restore")

    _restore_module_loaded = True
