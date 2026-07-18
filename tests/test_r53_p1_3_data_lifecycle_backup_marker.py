"""R53 P1-3: Data Lifecycle 严格 Backup Marker 参数用于真实清理测试。

被测目标:
- ``services.data_lifecycle._verify_backup_marker`` 严格校验(backup_id +
  user_coverage + checksum + completed_at 绑定)
- ``services.data_lifecycle.cleanup_expired_data`` 物理删除强制严格 backup marker,
  break-glass 审批,绑定审计日志

测试覆盖(7 项场景):
    1. 物理删除时 _verify_backup_marker 被调用且 require_user_scope=True,
       require_checksum=True
    2. 缺少 backup_id → 拒绝
    3. 缺少 manifest checksum → 拒绝
    4. backup marker 校验失败 → 拒绝物理删除
    5. skip_backup_check=True 无 break-glass 审批 → 拒绝
    6. skip_backup_check=True + break-glass 审批 → 允许(并记录审计)
    7. 批量全库备份时使用 manifest user coverage

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),通过 ``CacheStore.init()`` 创建表
- 直接 INSERT 测试数据到 users_local / file_records_local / user_data_retention
- 通过 ``unittest.mock.patch`` 模拟 BackupEngine.get_last_successful_backup
- 中文注释 + 中文日志,英文 raise 消息(遵循项目规范)
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
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r53_p1_3_test_")
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
# 测试数据辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_user(store, user_id: int, level: str = "free"):
    """插入测试用户到 users_local 表。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO users_local
           (user_id, username, first_name, membership_level,
            daily_decode_quota, quota_used_today, quota_date, can_upload,
            is_banned, created_at, updated_at, crdb_synced)
           VALUES (?, ?, ?, ?, ?, 0, ?, 1, 0, ?, ?, 1)""",
        (user_id, f"user_{user_id}", f"User{user_id}", level, 3, now, now, now),
    )
    await store._db.commit()


async def _insert_file_record(store, file_code: str, uploader_id: int):
    """插入测试 file_records_local 记录。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO file_records_local
           (file_code, uploader_id, primary_channel_id, primary_channel_msg_id,
            file_types, status, request_count, create_time, updated_at, crdb_synced)
           VALUES (?, ?, 100, 1, 'photo', 'active', 0, ?, ?, 1)""",
        (file_code, uploader_id, now, now),
    )
    await store._db.commit()


async def _setup_retention_and_soft_delete(store, user_id: int, days: int = 7,
                                            soft_delete_days_ago: int = 8):
    """设置保留期 + 插入已软删的 file_records_local。

    Args:
        user_id: 用户 ID
        days: 保留期天数
        soft_delete_days_ago: 软删时间(天前,需 > days 才会过保留期)
    """
    import datetime as _dt
    from services import data_lifecycle

    await data_lifecycle._ensure_retention_table()
    await data_lifecycle.set_retention(user_id=user_id, days=days)

    file_code = f"FC_R53_P1_3_U{user_id}"
    await _insert_file_record(store, file_code, user_id)
    # 手动设置 deleted_at 为 N 天前(超过保留期)
    old_dt = (_dt.datetime.now() - _dt.timedelta(days=soft_delete_days_ago)).isoformat()
    await store._db.execute(
        "UPDATE file_records_local SET deleted_at = ? WHERE file_code = ?",
        (old_dt, file_code),
    )
    await store._db.commit()


def _make_backup_record(
    backup_id: str = "backup_r53_p1_3_001",
    completed_at: str = "2026-07-15T10:00:00",
    checksum: str = "abc123def456",
    user_coverage: list | None = None,
    user_id: int | None = None,
) -> dict:
    """构造一个成功的 backup_history 记录(get_last_successful_backup 返回值)。

    Args:
        backup_id: 备份 ID
        completed_at: 完成时间
        checksum: manifest checksum
        user_coverage: 全库备份覆盖的用户 ID 列表
        user_id: 单用户备份的 user_id(与 user_coverage 互斥)
    """
    record = {
        "backup_id": backup_id,
        "created_at": completed_at,
        "completed_at": completed_at,
        "size": 1024,
        "encrypted": True,
        "key_id": "kek_v1",
        "checksum": checksum,
        "status": "completed",
        "complete_marker_exists": True,
        "backup_type": "full",
        "tables": 10,
        "total_rows": 100,
    }
    if user_coverage is not None:
        record["user_coverage"] = user_coverage
    if user_id is not None:
        record["user_id"] = user_id
    return record


# ════════════════════════════════════════════════════════════════
# 场景 1: 物理删除时 _verify_backup_marker 被调用且 require_user_scope=True,
#          require_checksum=True
# ════════════════════════════════════════════════════════════════

class TestStrictBackupMarkerEnforced:
    """R53 P1-3: 物理删除强制严格 backup marker 校验。"""

    @pytest.mark.asyncio
    async def test_verify_backup_marker_called_with_strict_params(self, real_store):
        """测试: 物理删除时 _verify_backup_marker 被调用且
        require_user_scope=True, require_checksum=True。"""
        from services import data_lifecycle

        user_id = 53001
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock _verify_backup_marker 返回有效 backup_info(含 user_coverage 覆盖该用户)
        backup_info = {
            "backup_id": "backup_strict_001",
            "checksum": "sha256_strict_001",
            "completed_at": "2026-07-15T10:00:00",
            "user_coverage": [user_id],
        }

        with patch.object(
            data_lifecycle, "_verify_backup_marker",
            new=AsyncMock(return_value=backup_info),
        ) as mock_verify:
            cleaned = await data_lifecycle.cleanup_expired_data(batch_size=10)

        # 验证 _verify_backup_marker 被调用
        assert mock_verify.called, "_verify_backup_marker 应被调用"
        # 验证 require_user_scope=True, require_checksum=True
        call_kwargs = mock_verify.call_args.kwargs
        assert call_kwargs.get("require_user_scope") is True, (
            "require_user_scope 应为 True"
        )
        assert call_kwargs.get("require_checksum") is True, (
            "require_checksum 应为 True"
        )
        # 验证清理成功(用户在 user_coverage 中)
        assert cleaned >= 1, "应清理至少 1 条 file_records_local"

        # 验证记录已物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "file_records_local 应已物理删除"


# ════════════════════════════════════════════════════════════════
# 场景 2: 缺少 backup_id → 拒绝
# ════════════════════════════════════════════════════════════════

class TestMissingBackupIdRejected:
    """R53 P1-3: backup marker 缺少 backup_id → 拒绝物理删除。"""

    @pytest.mark.asyncio
    async def test_missing_backup_id_rejected(self, real_store):
        """测试: backup 记录缺少 backup_id → _verify_backup_marker 返回 None →
        cleanup_expired_data raise AppError(BACKUP_MARKER_MISSING)。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        user_id = 53002
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock get_last_successful_backup 返回缺少 backup_id 的记录
        record_without_id = _make_backup_record(backup_id="")
        # backup_id 为空字符串,即"缺少 backup_id"

        with patch(
            "services.backup_engine.BackupEngine.get_last_successful_backup",
            new=AsyncMock(return_value=record_without_id),
        ):
            with pytest.raises(AppError) as exc_info:
                await data_lifecycle.cleanup_expired_data(batch_size=10)

        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING

        # 验证数据未被物理删除(因 backup marker 校验失败)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "backup_id 缺失时 file_records_local 不应被删除"


# ════════════════════════════════════════════════════════════════
# 场景 3: 缺少 manifest checksum → 拒绝
# ════════════════════════════════════════════════════════════════

class TestMissingChecksumRejected:
    """R53 P1-3: backup marker 缺少 checksum → 拒绝物理删除。"""

    @pytest.mark.asyncio
    async def test_missing_checksum_rejected(self, real_store):
        """测试: backup 记录缺少 checksum → _verify_backup_marker 返回 None →
        cleanup_expired_data raise AppError(BACKUP_MARKER_MISSING)。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        user_id = 53003
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock get_last_successful_backup 返回缺少 checksum 的记录
        record_without_checksum = _make_backup_record(checksum="")
        # require_checksum=True 时空 checksum 会被拒绝

        with patch(
            "services.backup_engine.BackupEngine.get_last_successful_backup",
            new=AsyncMock(return_value=record_without_checksum),
        ):
            with pytest.raises(AppError) as exc_info:
                await data_lifecycle.cleanup_expired_data(batch_size=10)

        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING

        # 验证数据未被物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "checksum 缺失时 file_records_local 不应被删除"


# ════════════════════════════════════════════════════════════════
# 场景 4: backup marker 校验失败 → 拒绝物理删除
# ════════════════════════════════════════════════════════════════

class TestBackupMarkerVerificationFailedRejected:
    """R53 P1-3: backup marker 校验失败 → 拒绝物理删除。"""

    @pytest.mark.asyncio
    async def test_backup_marker_none_rejected(self, real_store):
        """测试: _verify_backup_marker 返回 None(校验失败) →
        cleanup_expired_data raise AppError(BACKUP_MARKER_MISSING)。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        user_id = 53004
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock _verify_backup_marker 返回 None(校验失败)
        with patch.object(
            data_lifecycle, "_verify_backup_marker",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(AppError) as exc_info:
                await data_lifecycle.cleanup_expired_data(batch_size=10)

        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING

        # 验证数据未被物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "backup marker 校验失败时 file_records_local 不应被删除"

    @pytest.mark.asyncio
    async def test_no_successful_backup_rejected(self, real_store):
        """测试: get_last_successful_backup 返回 None(无成功备份) →
        _verify_backup_marker 返回 None → cleanup 拒绝。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        user_id = 53005
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock get_last_successful_backup 返回 None(无成功备份)
        with patch(
            "services.backup_engine.BackupEngine.get_last_successful_backup",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(AppError) as exc_info:
                await data_lifecycle.cleanup_expired_data(batch_size=10)

        assert exc_info.value.code == ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING


# ════════════════════════════════════════════════════════════════
# 场景 5: skip_backup_check=True 无 break-glass 审批 → 拒绝
# ════════════════════════════════════════════════════════════════

class TestSkipBackupCheckWithoutBreakGlassRejected:
    """R53 P1-3: skip_backup_check=True 无 break-glass 审批 → 拒绝。"""

    @pytest.mark.asyncio
    async def test_skip_backup_check_without_break_glass_rejected(
        self, real_store, monkeypatch,
    ):
        """测试: skip_backup_check=True 无 BREAK_GLASS_APPROVED 环境变量且
        无 approval_action_id → raise AppError(BREAK_GLASS_APPROVAL_REQUIRED)。"""
        from services import data_lifecycle
        from services.error_codes import AppError, ErrorCodes

        user_id = 53006
        await _setup_retention_and_soft_delete(real_store, user_id)

        # 确保没有 BREAK_GLASS_APPROVED 环境变量
        monkeypatch.delenv("BREAK_GLASS_APPROVED", raising=False)

        with pytest.raises(AppError) as exc_info:
            await data_lifecycle.cleanup_expired_data(
                batch_size=10, skip_backup_check=True,
                # 不传 approval_action_id
            )

        assert exc_info.value.code == (
            ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED
        ), (
            f"应抛 BREAK_GLASS_APPROVAL_REQUIRED,实际: {exc_info.value.code}"
        )

        # 验证数据未被物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "无 break-glass 审批时 file_records_local 不应被删除"


# ════════════════════════════════════════════════════════════════
# 场景 6: skip_backup_check=True + break-glass 审批 → 允许(并记录审计)
# ════════════════════════════════════════════════════════════════

class TestSkipBackupCheckWithBreakGlassAllowed:
    """R53 P1-3: skip_backup_check=True + break-glass 审批 → 允许并记录审计。"""

    @pytest.mark.asyncio
    async def test_skip_backup_check_with_env_break_glass_allowed(
        self, real_store, monkeypatch,
    ):
        """R54 P0-3: skip_backup_check=True + BREAK_GLASS_APPROVED 环境变量
        不再被接受,必须走真实 CommandBus 审批路径。
        本测试改用真实审批:插入 approved 状态的 command_executions 记录,
        调用 cleanup_expired_data(skip_backup_check=True, approval_action_id=...),
        claim_execution_approved CAS approved→executing 成功 → 允许清理 + 写审计日志。"""
        from services import data_lifecycle
        from services import command_bus

        user_id = 53007
        await _setup_retention_and_soft_delete(real_store, user_id)

        # R54 P0-3: 删除环境变量授权路径,改用真实 CommandBus 审批
        # 确保 BREAK_GLASS_APPROVED 环境变量不存在(验证不再走 env 路径)
        monkeypatch.delenv("BREAK_GLASS_APPROVED", raising=False)

        # 插入 approved 状态的 command_executions 记录(真实审批)
        action_id = "approval_break_glass_env_r54_p0_3"
        now = command_bus._now_iso()
        await real_store._db.execute(
            "INSERT INTO command_executions "
            "(action_id, command_type, principal_id, status, owner, lease_until, "
            "request_hash, result, created_at, updated_at, requires_approval, approved_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?)",
            (
                action_id, "data_lifecycle_break_glass", 1,
                command_bus.CMD_STATUS_APPROVED,
                "a" * 64, now, now, 1, now,
            ),
        )
        await real_store._db.commit()
        # R56 P0-2: 插入 command_approvals 表 2 条 break_glass 审批记录(双人审批)
        # 要求:≥2 个不同 approver + mfa_receipt 非空 + 非自审批(≠ principal_id=1)
        # R58 P0-2: 新增强制字段 decision/request_hash/expires_at/consumed_at/revoked_at
        await real_store._db.execute(
            "CREATE TABLE IF NOT EXISTS command_approvals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "action_id TEXT NOT NULL, "
            "approver_id BIGINT NOT NULL, "
            "approval_type TEXT NOT NULL, "
            "mfa_receipt TEXT, "
            "approved_at TEXT NOT NULL, "
            "metadata_json TEXT, "
            "UNIQUE(action_id, approver_id))"
        )
        # R58 P0-2: 补列(幂等,重复添加忽略)
        for col, col_def in [
            ("decision", "TEXT NOT NULL DEFAULT 'approved'"),
            ("request_hash", "TEXT NOT NULL DEFAULT ''"),
            ("permission", "TEXT NOT NULL DEFAULT ''"),
            ("expires_at", "TEXT NOT NULL DEFAULT ''"),
            ("consumed_at", "TEXT"),
            ("revoked_at", "TEXT"),
        ]:
            try:
                await real_store._db.execute(
                    f"ALTER TABLE command_approvals ADD COLUMN {col} {col_def}"
                )
            except Exception:
                pass  # 列已存在(幂等)
        # R58 P0-2: 插入完整字段的审批记录(decision=approved, request_hash=64hex, 未过期/未消费/未撤销)
        # R59 P0-02: expires_at 必须是 UTC aware ISO(带 +00:00 后缀),
        # 因 _parse_iso_utc 将无 tz 后缀的视为 UTC。
        # R59 P0-03: mfa_receipt 必须是真实签发的 PASETO-like token,
        # 不能再用占位字符串。
        import datetime as _dt_mod
        from admin.mfa import issue_mfa_receipt
        # 设置 MFA receipt 签名密钥(测试用固定密钥)
        monkeypatch.setenv("MFA_RECEIPT_SIGNING_KEY", "test_signing_key_for_r59_p0_03_only_32b")
        future_iso = (_dt_mod.datetime.now(_dt_mod.timezone.utc)
                      + _dt_mod.timedelta(hours=1)).isoformat()
        canonical_hash = "a" * 64
        # R59 P0-03: 为每个 approver 签发真实 MFA receipt
        receipt_approver_2 = issue_mfa_receipt(
            principal_id=2, purpose="break_glass_approval",
            action_hash=canonical_hash, amr=["totp"], ttl_seconds=300,
        )
        receipt_approver_3 = issue_mfa_receipt(
            principal_id=3, purpose="break_glass_approval",
            action_hash=canonical_hash, amr=["totp"], ttl_seconds=300,
        )
        await real_store._db.execute(
            "INSERT INTO command_approvals "
            "(action_id, approver_id, approval_type, mfa_receipt, approved_at, metadata_json, "
            "decision, request_hash, permission, expires_at, consumed_at, revoked_at) "
            "VALUES (?, ?, 'break_glass', ?, ?, NULL, 'approved', ?, 'break_glass', ?, NULL, NULL)",
            (action_id, 2, receipt_approver_2, now, canonical_hash, future_iso),
        )
        await real_store._db.execute(
            "INSERT INTO command_approvals "
            "(action_id, approver_id, approval_type, mfa_receipt, approved_at, metadata_json, "
            "decision, request_hash, permission, expires_at, consumed_at, revoked_at) "
            "VALUES (?, ?, 'break_glass', ?, ?, NULL, 'approved', ?, 'break_glass', ?, NULL, NULL)",
            (action_id, 3, receipt_approver_3, now, canonical_hash, future_iso),
        )
        await real_store._db.commit()
        # R61 P0-01: 新 UoW 调用 check_permission(principal_id, grant.permission)
        # 需为 principal_id=1 bootstrap super_admin(["*"] 通配)使权限重鉴权通过
        await real_store.bootstrap_admin_principal(
            principal_id=1, username="admin", roles=["super_admin"],
        )

        cleaned = await data_lifecycle.cleanup_expired_data(
            batch_size=10, skip_backup_check=True,
            approval_action_id=action_id,
            request_hash=canonical_hash,
            principal_id=1,
        )
        assert cleaned >= 1, "真实审批后应清理至少 1 条记录"

        # 验证记录已物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "真实审批后 file_records_local 应已物理删除"

        # 验证审计日志已写入(R55 P0-6: action=physical_delete_per_user,同事务原子写入)
        cursor = await real_store._db.execute(
            "SELECT action, details FROM audit_log "
            "WHERE action = 'physical_delete_per_user' "
            "ORDER BY id DESC LIMIT 1",
        )
        arow = await cursor.fetchone()
        assert arow is not None, "应写入 physical_delete_per_user 审计日志"
        details = json.loads(arow[1]) if arow[1] else {}
        assert details.get("deletion_status") == "completed", (
            f"deletion_status 应为 completed,实际: {details.get('deletion_status')}"
        )

    @pytest.mark.asyncio
    async def test_skip_backup_check_with_approval_action_id_allowed(
        self, real_store, monkeypatch,
    ):
        """R54 P0-3: skip_backup_check=True + approval_action_id →
        必须通过 claim_execution_approved CAS 真实审批。
        本测试先插入 approved 状态的 command_executions 记录,
        调用 cleanup_expired_data 时 claim_execution_approved CAS approved→executing 成功 →
        允许清理 + 写审计日志。"""
        from services import data_lifecycle
        from services import command_bus

        user_id = 53008
        await _setup_retention_and_soft_delete(real_store, user_id)

        # 确保没有 BREAK_GLASS_APPROVED 环境变量(强制走 approval_action_id 路径)
        monkeypatch.delenv("BREAK_GLASS_APPROVED", raising=False)

        # R54 P0-3: 插入 approved 状态的 command_executions 记录(真实审批)
        # claim_execution_approved 会 CAS approved→executing,记录必须处于 approved 状态
        action_id = "approval_break_glass_r53_p1_3"
        now = command_bus._now_iso()
        await real_store._db.execute(
            "INSERT INTO command_executions "
            "(action_id, command_type, principal_id, status, owner, lease_until, "
            "request_hash, result, created_at, updated_at, requires_approval, approved_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?)",
            (
                action_id, "data_lifecycle_break_glass", 1,
                command_bus.CMD_STATUS_APPROVED,
                "a" * 64, now, now, 1, now,
            ),
        )
        await real_store._db.commit()
        # R56 P0-2: 插入 command_approvals 表 2 条 break_glass 审批记录(双人审批)
        # R58 P0-2: 新增强制字段 decision/request_hash/expires_at/consumed_at/revoked_at
        await real_store._db.execute(
            "CREATE TABLE IF NOT EXISTS command_approvals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "action_id TEXT NOT NULL, "
            "approver_id BIGINT NOT NULL, "
            "approval_type TEXT NOT NULL, "
            "mfa_receipt TEXT, "
            "approved_at TEXT NOT NULL, "
            "metadata_json TEXT, "
            "UNIQUE(action_id, approver_id))"
        )
        # R58 P0-2: 补列(幂等)
        for col, col_def in [
            ("decision", "TEXT NOT NULL DEFAULT 'approved'"),
            ("request_hash", "TEXT NOT NULL DEFAULT ''"),
            ("permission", "TEXT NOT NULL DEFAULT ''"),
            ("expires_at", "TEXT NOT NULL DEFAULT ''"),
            ("consumed_at", "TEXT"),
            ("revoked_at", "TEXT"),
        ]:
            try:
                await real_store._db.execute(
                    f"ALTER TABLE command_approvals ADD COLUMN {col} {col_def}"
                )
            except Exception:
                pass  # 列已存在(幂等)
        # R58 P0-2: 插入完整字段的审批记录(decision=approved, request_hash=64hex, 未过期/未消费/未撤销)
        # R59 P0-02: expires_at 必须是 UTC aware ISO(带 +00:00 后缀)
        # R59 P0-03: mfa_receipt 必须是真实签发的 PASETO-like token
        import datetime as _dt_mod
        from admin.mfa import issue_mfa_receipt
        monkeypatch.setenv("MFA_RECEIPT_SIGNING_KEY", "test_signing_key_for_r59_p0_03_only_32b")
        future_iso = (_dt_mod.datetime.now(_dt_mod.timezone.utc)
                      + _dt_mod.timedelta(hours=1)).isoformat()
        canonical_hash = "a" * 64
        receipt_approver_2 = issue_mfa_receipt(
            principal_id=2, purpose="break_glass_approval",
            action_hash=canonical_hash, amr=["totp"], ttl_seconds=300,
        )
        receipt_approver_3 = issue_mfa_receipt(
            principal_id=3, purpose="break_glass_approval",
            action_hash=canonical_hash, amr=["totp"], ttl_seconds=300,
        )
        await real_store._db.execute(
            "INSERT INTO command_approvals "
            "(action_id, approver_id, approval_type, mfa_receipt, approved_at, metadata_json, "
            "decision, request_hash, permission, expires_at, consumed_at, revoked_at) "
            "VALUES (?, ?, 'break_glass', ?, ?, NULL, 'approved', ?, 'break_glass', ?, NULL, NULL)",
            (action_id, 2, receipt_approver_2, now, canonical_hash, future_iso),
        )
        await real_store._db.execute(
            "INSERT INTO command_approvals "
            "(action_id, approver_id, approval_type, mfa_receipt, approved_at, metadata_json, "
            "decision, request_hash, permission, expires_at, consumed_at, revoked_at) "
            "VALUES (?, ?, 'break_glass', ?, ?, NULL, 'approved', ?, 'break_glass', ?, NULL, NULL)",
            (action_id, 3, receipt_approver_3, now, canonical_hash, future_iso),
        )
        await real_store._db.commit()
        # R61 P0-01: 新 UoW 调用 check_permission(principal_id, grant.permission)
        # 需为 principal_id=1 bootstrap super_admin(["*"] 通配)使权限重鉴权通过
        await real_store.bootstrap_admin_principal(
            principal_id=1, username="admin", roles=["super_admin"],
        )

        cleaned = await data_lifecycle.cleanup_expired_data(
            batch_size=10, skip_backup_check=True,
            approval_action_id=action_id,
            request_hash=canonical_hash,
            principal_id=1,
        )
        assert cleaned >= 1, "approval_action_id 审批后应清理至少 1 条记录"

        # 验证记录已物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "approval_action_id 审批后 file_records_local 应已物理删除"

        # 验证审计日志已写入(R55 P0-6: action=physical_delete_per_user,同事务原子写入)
        cursor = await real_store._db.execute(
            "SELECT action, details FROM audit_log "
            "WHERE action = 'physical_delete_per_user' "
            "ORDER BY id DESC LIMIT 1",
        )
        arow = await cursor.fetchone()
        assert arow is not None, "应写入 physical_delete_per_user 审计日志"
        details = json.loads(arow[1]) if arow[1] else {}
        assert details.get("deletion_status") == "completed", (
            f"deletion_status 应为 completed,实际: {details.get('deletion_status')}"
        )


# ════════════════════════════════════════════════════════════════
# 场景 7: 批量全库备份时使用 manifest user coverage
# ════════════════════════════════════════════════════════════════

class TestBatchFullBackupUserCoverage:
    """R53 P1-3: 批量全库备份使用 manifest 中的 user coverage。"""

    @pytest.mark.asyncio
    async def test_user_coverage_covers_user_allows_delete(self, real_store):
        """测试: user_coverage 包含目标用户 → 允许物理删除。"""
        from services import data_lifecycle

        user_id = 53009
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock _verify_backup_marker 返回含 user_coverage 的 backup_info
        # user_coverage 包含目标用户
        backup_info = {
            "backup_id": "backup_coverage_001",
            "checksum": "sha256_coverage_001",
            "completed_at": "2026-07-15T10:00:00",
            "user_coverage": [53009, 53010, 53011],
        }

        with patch.object(
            data_lifecycle, "_verify_backup_marker",
            new=AsyncMock(return_value=backup_info),
        ):
            cleaned = await data_lifecycle.cleanup_expired_data(batch_size=10)

        # 用户在 user_coverage 中 → 应清理
        assert cleaned >= 1, "用户在 user_coverage 中应被清理"

        # 验证记录已物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "user_coverage 覆盖的用户数据应已物理删除"

    @pytest.mark.asyncio
    async def test_user_coverage_excludes_user_skips_delete(self, real_store):
        """测试: user_coverage 不包含目标用户 → 跳过物理删除。"""
        from services import data_lifecycle

        user_id = 53010
        await _setup_retention_and_soft_delete(real_store, user_id)

        # Mock _verify_backup_marker 返回含 user_coverage 的 backup_info
        # user_coverage 不包含目标用户(只有 53099, 不含 53010)
        backup_info = {
            "backup_id": "backup_coverage_002",
            "checksum": "sha256_coverage_002",
            "completed_at": "2026-07-15T10:00:00",
            "user_coverage": [53099],  # 不包含 53010
        }

        with patch.object(
            data_lifecycle, "_verify_backup_marker",
            new=AsyncMock(return_value=backup_info),
        ):
            cleaned = await data_lifecycle.cleanup_expired_data(batch_size=10)

        # 用户不在 user_coverage 中 → 不应清理
        assert cleaned == 0, "用户不在 user_coverage 中不应被清理"

        # 验证记录未被物理删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_id}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "user_coverage 未覆盖的用户数据不应被删除"

    @pytest.mark.asyncio
    async def test_user_coverage_multiple_users_partial_coverage(self, real_store):
        """测试: 多用户场景下 user_coverage 只覆盖部分用户 →
        只清理覆盖的用户,跳过未覆盖的。"""
        from services import data_lifecycle

        # 设置两个用户,都有过期数据
        user_covered = 53011  # 在 user_coverage 中
        user_not_covered = 53012  # 不在 user_coverage 中
        await _setup_retention_and_soft_delete(real_store, user_covered)
        await _setup_retention_and_soft_delete(real_store, user_not_covered)

        # Mock _verify_backup_marker 返回含 user_coverage 的 backup_info
        backup_info = {
            "backup_id": "backup_coverage_003",
            "checksum": "sha256_coverage_003",
            "completed_at": "2026-07-15T10:00:00",
            "user_coverage": [user_covered],  # 只覆盖 53011
        }

        with patch.object(
            data_lifecycle, "_verify_backup_marker",
            new=AsyncMock(return_value=backup_info),
        ):
            cleaned = await data_lifecycle.cleanup_expired_data(batch_size=10)

        # 只应清理覆盖用户的数据
        assert cleaned >= 1, "应清理覆盖用户的数据"

        # 验证覆盖用户的数据已删除
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_covered}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "覆盖用户的数据应已物理删除"

        # 验证未覆盖用户的数据保留
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM file_records_local WHERE file_code = ?",
            (f"FC_R53_P1_3_U{user_not_covered}",),
        )
        row = await cursor.fetchone()
        assert row[0] == 1, "未覆盖用户的数据不应被删除"

    @pytest.mark.asyncio
    async def test_physical_delete_writes_binding_audit_log(self, real_store):
        """测试: 物理删除时写入绑定 backup_id/checksum/completed_at/retention_cutoff
        的审计日志。"""
        from services import data_lifecycle

        user_id = 53013
        await _setup_retention_and_soft_delete(real_store, user_id, days=7)

        # Mock _verify_backup_marker 返回含完整绑定信息的 backup_info
        backup_info = {
            "backup_id": "backup_binding_001",
            "checksum": "sha256_binding_001",
            "completed_at": "2026-07-15T10:00:00",
            "user_coverage": [user_id],
        }

        with patch.object(
            data_lifecycle, "_verify_backup_marker",
            new=AsyncMock(return_value=backup_info),
        ):
            await data_lifecycle.cleanup_expired_data(batch_size=10)

        # 验证绑定审计日志已写入
        cursor = await real_store._db.execute(
            "SELECT action, details FROM audit_log "
            "WHERE action = 'physical_delete_per_user' "
            "ORDER BY id DESC LIMIT 1",
        )
        arow = await cursor.fetchone()
        assert arow is not None, "应写入 physical_delete_per_user 审计日志"
        details = json.loads(arow[1]) if arow[1] else {}

        # 验证绑定字段都存在
        assert details.get("backup_id") == "backup_binding_001", (
            f"backup_id 应绑定,实际: {details.get('backup_id')}"
        )
        assert details.get("checksum") == "sha256_binding_001", (
            f"checksum 应绑定,实际: {details.get('checksum')}"
        )
        assert details.get("completed_at") == "2026-07-15T10:00:00", (
            f"completed_at 应绑定,实际: {details.get('completed_at')}"
        )
        assert "retention_cutoff" in details, "retention_cutoff 应存在于审计日志"
        assert details.get("retention_days") == 7, (
            f"retention_days 应为 7,实际: {details.get('retention_days')}"
        )
        assert details.get("user_id") == user_id, (
            f"user_id 应为 {user_id},实际: {details.get('user_id')}"
        )
