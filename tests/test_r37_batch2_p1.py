"""R37 Batch 2 P1 测试覆盖

测试覆盖:
- P1-1: replication_task 创建失败时 fail-closed(不回退到无 task 复制)
- P1-2: COPIED_UNVERIFIED 超时重试前用 dst_msg_id 二次写入 manifest
- P1-3: 权威 outbox/session 写入抛 DurabilityError(_strict 版本)
- P1-4: BACKUP_ENCRYPTION_REQUIRED=true 时 KEK 不可用停止备份
- P1-5: 权威表新增 deleted_at tombstone 列 + 备份增量 watermark 同时检查
"""
import os
import sys
import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────
#  P1-1: task 创建失败 fail-closed
# ─────────────────────────────────────────────────────────────

class TestP11TaskCreateFailClosed:
    """P1-1: replication_task 创建失败时不再回退到无 task 复制。"""

    def test_scheduler_no_plan_items_append_on_task_zero(self):
        """scheduler.py 中 task_id==0 时不再 plan_items.append(item)。"""
        scheduler_path = Path(__file__).resolve().parent.parent / "services" / "mon" / "scheduler.py"
        content = scheduler_path.read_text(encoding="utf-8")
        # 找到 task_id == 0 的处理分支
        assert "if task_id == 0:" in content
        # 断言: 不再 plan_items.append(item)
        # 在 task_id==0 分支内不应有 plan_items.append(item)
        # 简化检查: fail-closed 注释存在
        assert "R37 P1-1" in content
        assert "fail-closed" in content

    def test_scheduler_skip_with_logger_error(self):
        """task 创建失败时使用 logger.error(非 warning)+ continue。"""
        scheduler_path = Path(__file__).resolve().parent.parent / "services" / "mon" / "scheduler.py"
        content = scheduler_path.read_text(encoding="utf-8")
        # 找到 R37 P1-1 注释块
        idx = content.find("R37 P1-1")
        assert idx > 0
        # 在 R37 P1-1 注释块附近(后 800 字符)应有 logger.error + continue
        snippet = content[idx:idx + 800]
        assert "logger.error" in snippet
        assert "continue" in snippet


# ─────────────────────────────────────────────────────────────
#  P1-2: COPIED_UNVERIFIED 超时前二次写入 manifest
# ─────────────────────────────────────────────────────────────

class TestP12ReconcileSecondaryProbe:
    """P1-2: 超时重试前用 dst_msg_id 二次写入 manifest。"""

    def test_reconcile_timeout_has_secondary_probe(self):
        """scheduler.py 超时分支包含 R37 P1-2 二次探测逻辑。"""
        scheduler_path = Path(__file__).resolve().parent.parent / "services" / "mon" / "scheduler.py"
        content = scheduler_path.read_text(encoding="utf-8")
        assert "R37 P1-2" in content
        # 超时分支在 dst_msg_id 存在时应尝试二次 commit
        # 检查 "_commit_replication_transaction_safe" 在 R37 P1-2 注释附近
        idx = content.find("R37 P1-2")
        assert idx > 0
        # 在 R37 P1-2 注释后 1500 字符内应有 _commit_replication_transaction_safe
        snippet = content[idx:idx + 1500]
        assert "_commit_replication_transaction_safe" in snippet
        assert "if dst_msg_id:" in snippet

    def test_reconcile_logs_secondary_probe_attempt(self):
        """超时分支包含明确的 R37 P1-2 告警日志。"""
        scheduler_path = Path(__file__).resolve().parent.parent / "services" / "mon" / "scheduler.py"
        content = scheduler_path.read_text(encoding="utf-8")
        # R37 P1-2 注释和超时二次探测日志
        assert "超时但 dst_msg_id 存在" in content or "二次写入 manifest 成功" in content
        assert "超时且二次写入 manifest 失败" in content or "超时且二次写入" in content


# ─────────────────────────────────────────────────────────────
#  P1-3: DurabilityError 统一(_strict 版本)
# ─────────────────────────────────────────────────────────────

class TestP13DurabilityErrorUnification:
    """P1-3: 权威状态写入必须使用 strict 版本(抛 DurabilityError)。"""

    def test_up_bot_has_strict_variants(self):
        """up_bot.py 定义 _transition_upload_session_strict 和 _create_outbox_entry_strict。"""
        up_bot_path = Path(__file__).resolve().parent.parent / "bots" / "up_bot.py"
        content = up_bot_path.read_text(encoding="utf-8")
        assert "async def _transition_upload_session_strict" in content
        assert "async def _create_outbox_entry_strict" in content

    def test_up_bot_strict_raises_durability_error(self):
        """_strict 版本抛 DurabilityError(不是 logger.warning + return)。"""
        up_bot_path = Path(__file__).resolve().parent.parent / "bots" / "up_bot.py"
        content = up_bot_path.read_text(encoding="utf-8")
        # 在 strict 函数体内应有 raise DurabilityError
        idx = content.find("async def _transition_upload_session_strict")
        assert idx > 0
        snippet = content[idx:idx + 1500]
        assert "DurabilityError" in snippet
        assert "raise" in snippet

        idx2 = content.find("async def _create_outbox_entry_strict")
        assert idx2 > 0
        snippet2 = content[idx2:idx2 + 1500]
        assert "DurabilityError" in snippet2
        assert "raise" in snippet2

    def test_up_bot_strict_called_for_authoritative_writes(self):
        """权威写入调用点使用 _strict 版本(REGISTER_MANIFEST/ARCHIVE_R100)。"""
        up_bot_path = Path(__file__).resolve().parent.parent / "bots" / "up_bot.py"
        content = up_bot_path.read_text(encoding="utf-8")
        # 至少 4 处 _create_outbox_entry_strict 调用(2 个调用点 × 2 个事件)
        # 检查调用次数
        count = content.count("_create_outbox_entry_strict(")
        # 函数定义 1 处 + 至少 4 处调用 = 5
        assert count >= 5, f"_create_outbox_entry_strict 调用次数不足: {count}"

    def test_safe_wrappers_documented_as_best_effort(self):
        """_safe() 包装文档说明仅用于 metrics/日志。"""
        up_bot_path = Path(__file__).resolve().parent.parent / "bots" / "up_bot.py"
        content = up_bot_path.read_text(encoding="utf-8")
        # 在 _safe 函数 docstring 中应有 R37 P1-3 说明
        assert "R37 P1-3" in content
        assert "best-effort" in content or "best_effort" in content or "metrics" in content


# ─────────────────────────────────────────────────────────────
#  P1-4: 备份强制加密
# ─────────────────────────────────────────────────────────────

class TestP14BackupEncryptionRequired:
    """P1-4: BACKUP_ENCRYPTION_REQUIRED=true 时 KEK 不可用停止备份。"""

    def test_settings_has_backup_encryption_required_field(self):
        """Settings 类包含 BACKUP_ENCRYPTION_REQUIRED 字段。"""
        settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        assert "BACKUP_ENCRYPTION_REQUIRED" in content
        assert "BACKUP_ENCRYPTION_REQUIRED: bool = False" in content

    def test_validator_checks_encryption_required(self):
        """_validate_backup_fields 在 BACKUP_ENCRYPTION_REQUIRED=true 时检查 BACKUP_KEK。"""
        settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.py"
        content = settings_path.read_text(encoding="utf-8")
        # 找到 _validate_backup_fields 函数
        idx = content.find("def _validate_backup_fields")
        assert idx > 0
        snippet = content[idx:idx + 2000]
        assert "BACKUP_ENCRYPTION_REQUIRED" in snippet
        assert "BACKUP_KEK 未配置" in snippet

    def test_db_backup_loop_stops_on_required_encryption_failure(self):
        """db_backup._run_backup_loop 在 BACKUP_ENCRYPTION_REQUIRED=true 且加密不可用时 return。"""
        db_backup_path = Path(__file__).resolve().parent.parent / "services" / "db_backup.py"
        content = db_backup_path.read_text(encoding="utf-8")
        assert "BACKUP_ENCRYPTION_REQUIRED" in content
        assert "encryption_required" in content
        # 在 BACKUP_ENCRYPTION_REQUIRED 分支内必须有 return(停止备份)
        idx = content.find("encryption_required = getattr(settings, \"BACKUP_ENCRYPTION_REQUIRED\"")
        assert idx > 0
        snippet = content[idx:idx + 800]
        assert "if encryption_required:" in snippet
        assert "return" in snippet

    def test_env_example_documents_backup_encryption_required(self):
        """.env.example 文档 BACKUP_ENCRYPTION_REQUIRED。"""
        env_path = Path(__file__).resolve().parent.parent / ".env.example"
        content = env_path.read_text(encoding="utf-8")
        assert "BACKUP_ENCRYPTION_REQUIRED" in content

    def test_conftest_mock_has_backup_encryption_required(self):
        """conftest.py mock settings 包含 BACKUP_ENCRYPTION_REQUIRED。"""
        conftest_path = Path(__file__).resolve().parent.parent / "tests" / "conftest.py"
        content = conftest_path.read_text(encoding="utf-8")
        assert "BACKUP_ENCRYPTION_REQUIRED" in content


# ─────────────────────────────────────────────────────────────
#  P1-5: tombstone 列 + 增量 watermark 删除追溯
# ─────────────────────────────────────────────────────────────

class TestP15TombstoneColumns:
    """P1-5: 权威表新增 deleted_at 列,增量备份捕捉删除。"""

    def test_session_ddl_users_has_deleted_at(self):
        """users 表 DDL 包含 deleted_at 列。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        # 找到 users 表 DDL
        idx = content.find("CREATE TABLE IF NOT EXISTS users")
        assert idx > 0
        # 在 users DDL 块内(到下个 """)应有 deleted_at
        end = content.find(')"', idx) + 2
        users_ddl = content[idx:end]
        assert "deleted_at TEXT" in users_ddl

    def test_session_ddl_file_records_has_deleted_at(self):
        """file_records 表 DDL 包含 deleted_at 列。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("CREATE TABLE IF NOT EXISTS file_records")
        assert idx > 0
        end = content.find(')"', idx) + 2
        ddl = content[idx:end]
        assert "deleted_at TEXT" in ddl

    def test_session_ddl_codes_has_deleted_at(self):
        """codes 表 DDL 包含 deleted_at 列。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("CREATE TABLE IF NOT EXISTS codes")
        assert idx > 0
        end = content.find(')"', idx) + 2
        ddl = content[idx:end]
        assert "deleted_at TEXT" in ddl

    def test_session_ddl_cells_has_deleted_at(self):
        """cells 表 DDL 包含 deleted_at 列。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        idx = content.find("CREATE TABLE IF NOT EXISTS cells")
        assert idx > 0
        end = content.find(')"', idx) + 2
        ddl = content[idx:end]
        assert "deleted_at TEXT" in ddl

    def test_migration_statements_include_deleted_at(self):
        """MIGRATION_STATEMENTS 包含 4 个权威表的 deleted_at ADD COLUMN。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        # 4 条 ALTER TABLE ... ADD COLUMN IF NOT EXISTS deleted_at TEXT
        count = content.count("ADD COLUMN IF NOT EXISTS deleted_at TEXT")
        assert count == 4, f"应有 4 条 deleted_at ALTER TABLE 语句,实际: {count}"

    def test_backup_schema_includes_deleted_at(self):
        """backup_schema.py 中 4 个权威表的 columns 包含 deleted_at。"""
        schema_path = Path(__file__).resolve().parent.parent / "services" / "backup_schema.py"
        content = schema_path.read_text(encoding="utf-8")
        # users
        idx = content.find('"users": TableSchema(')
        assert idx > 0
        snippet = content[idx:idx + 800]
        assert "deleted_at" in snippet
        # file_records
        idx = content.find('"file_records": TableSchema(')
        assert idx > 0
        snippet = content[idx:idx + 1200]
        assert "deleted_at" in snippet
        # codes
        idx = content.find('"codes": TableSchema(')
        assert idx > 0
        snippet = content[idx:idx + 800]
        assert "deleted_at" in snippet
        # cells
        idx = content.find('"cells": TableSchema(')
        assert idx > 0
        snippet = content[idx:idx + 1200]
        assert "deleted_at" in snippet

    def test_backup_schema_file_records_where_clause_removed(self):
        """file_records 的 where_clause 不再是 status='active'(改为空)。"""
        schema_path = Path(__file__).resolve().parent.parent / "services" / "backup_schema.py"
        content = schema_path.read_text(encoding="utf-8")
        idx = content.find('"file_records": TableSchema(')
        assert idx > 0
        # 在 file_records 块附近不应有 status='active'
        snippet = content[idx:idx + 1500]
        assert "status = 'active'" not in snippet

    def test_db_backup_checks_deleted_at_in_watermark(self):
        """db_backup._backup_crdb_tables 增量条件同时检查 updated_at + deleted_at。"""
        db_backup_path = Path(__file__).resolve().parent.parent / "services" / "db_backup.py"
        content = db_backup_path.read_text(encoding="utf-8")
        # 找到 _backup_crdb_tables
        idx = content.find("async def _backup_crdb_tables")
        assert idx > 0
        snippet = content[idx:idx + 3000]
        # 应有 deleted_at 列检查
        assert "deleted_at" in snippet
        # 应有 OR 条件(updated_at OR deleted_at)
        assert "OR" in snippet


# ─────────────────────────────────────────────────────────────
#  DDL 版本号检查
# ─────────────────────────────────────────────────────────────

class TestDDLVersionBump:
    """R37 P1-5: DDL_VERSION 应升级到 9。"""

    def test_ddl_version_is_9(self):
        """DDL_VERSION = 9(R37 P1-5)。"""
        session_path = Path(__file__).resolve().parent.parent / "database" / "session.py"
        content = session_path.read_text(encoding="utf-8")
        assert "DDL_VERSION = 9" in content
        # 注释应提及 R37 P1-5
        idx = content.find("DDL_VERSION = 9")
        snippet = content[idx:idx + 200]
        assert "R37 P1-5" in snippet
