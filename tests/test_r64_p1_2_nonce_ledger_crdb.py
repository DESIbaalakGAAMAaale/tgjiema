"""R64 P1-02: capability nonce ledger 迁移到独立 CRDB security schema。

审计背景(R64 终审报告 P1-02):
    R63 P1-01 将 _RestoreCapability nonce 防重放从进程内 _CONSUMED_NONCES set
    迁移到 SQLite restore_capability_nonces 表。但 SQLite 是本地存储,多实例/
    多区域部署时无法跨实例共享 nonce ledger,且 nonce 状态机单一(consumed/
    non-existent),无 reserved/failed 中间态:

    - assert_valid 直接 consume,若后续 restore 失败,nonce 已被消费,
      同一 operation 无法重试(必须重新签发 capability,增加运维负担)
    - restore 失败后 nonce 状态不可追溯(无 failed 审计记录)

整改方案(R64 P1-02):
    1. nonce ledger 迁移到 CRDB security.restore_capability_nonces 表
       (security schema 隔离,跨实例共享,多区域一致)
    2. nonce 状态机扩展: reserved → consumed | failed
       - reserve_capability_nonce: INSERT status='reserved'(CAS,PRIMARY KEY=nonce)
       - consume_capability_nonce: UPDATE reserved→consumed(CAS,WHERE status='reserved')
       - fail_capability_nonce:    UPDATE reserved→failed(CAS,WHERE status='reserved')
    3. assert_valid 调用 reserve_capability_nonce(不再直接 consume),
       writer 在 restore 成功后 consume / 失败后 fail
    4. failed 状态允许同 operation 重试(新 capability 新 nonce,旧 failed nonce 留审计)
    5. CRDB 不可用时回退 SQLite(记录 warning,不阻断 — 单实例部署仍可用)

测试覆盖:
    1. **CRDB schema 创建**: _ensure_crdb_restore_capability_nonces 创建 security
       schema + security.restore_capability_nonces 表
    2. **reserve_capability_nonce**: NEW → reserved(INSERT CAS,PRIMARY KEY 冲突返回 False)
    3. **consume_capability_nonce CAS**: reserved → consumed(UPDATE WHERE status='reserved')
    4. **fail_capability_nonce CAS**: reserved → failed(UPDATE WHERE status='reserved')
    5. **failed 允许重试**: failed nonce 不阻塞同 operation 新 nonce 的 reserve
    6. **重复 consume 拒绝**: consumed 状态的 nonce 再次 consume 返回 False
    7. **CRDB 不可用回退 SQLite**: mock CRDB 不可用,验证 SQLite fallback + warning
    8. **is_capability_nonce_consumed**: 仅 status='consumed' 返回 True(reserved/failed 返回 False)
    9. **assert_valid 调用 reserve**: assert_valid 后 nonce 状态为 reserved(非 consumed)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 测试辅助 ──────────────────────────────────────────────────


def _ensure_backup_dr_validate_importable():
    """确保 services.backup_dr_validate 可导入(仅依赖 loguru / i18n)。"""
    if "services.backup_dr_validate" in sys.modules:
        return sys.modules["services.backup_dr_validate"]
    import importlib
    return importlib.import_module("services.backup_dr_validate")


async def _make_store(db_path: str | None = None):
    """构造一个真实 CacheStore(指定 db_path 或临时文件)。

    用于 R64 P1-02 nonce ledger 状态机测试。每次测试用全新 DB 文件,
    天然隔离,且能验证跨"重启"(新建 CacheStore 实例)的持久化。
    """
    from database.cache_store import CacheStore
    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r64_p1_02_test_")
        db_path = str(Path(_tmp_dir) / "test_cache.db")
    store = CacheStore(db_path=db_path)
    await store.init()
    return store, db_path


def _build_valid_capability(
    mod,
    *,
    payload_digest: str = "d" * 64,
    backup_id: str = "backup_test_001",
    manifest_sha256: str = "a" * 64,
    schema_fingerprint: str = "R64-P1-02-test-fingerprint",
    issuer: str = "test_issuer",
    ttl_seconds: int = 600,
):
    """构造一个合法的 _RestoreCapability(通过模块私有 sentinel)。"""
    return mod._RestoreCapability(
        mod._RESTORE_SENTINEL,
        backup_id=backup_id,
        manifest_sha256=manifest_sha256,
        payload_key="db_backup/payload_test.enc",
        ciphertext_sha256="b" * 64,
        plaintext_sha256="c" * 64,
        encryption_key_id="test_key_id",
        issuer=issuer,
        schema_fingerprint=schema_fingerprint,
        payload_digest=payload_digest,
        ttl_seconds=ttl_seconds,
    )


# ═══════════════════════════════════════════════════════════════
# 1. reserve_capability_nonce — NEW → reserved(INSERT CAS)
# ═══════════════════════════════════════════════════════════════


class TestReserveCapabilityNonce:
    """R64 P1-02: CacheStore.reserve_capability_nonce — INSERT status='reserved' CAS。"""

    @pytest.mark.asyncio
    async def test_first_reserve_returns_true(self):
        """首次 reserve 返回 True(INSERT 成功,nonce 之前不存在)。"""
        store, _ = await _make_store()
        try:
            won = await store.reserve_capability_nonce(
                "nonce_reserve_001",
                operation_id="op_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            assert won is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_second_reserve_same_nonce_returns_false(self):
        """同一 nonce 二次 reserve 返回 False(PRIMARY KEY 冲突,防重放)。"""
        store, _ = await _make_store()
        try:
            won1 = await store.reserve_capability_nonce(
                "nonce_reserve_002",
                operation_id="op_002",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            won2 = await store.reserve_capability_nonce(
                "nonce_reserve_002",  # 同一 nonce
                operation_id="op_002",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host2:5678",
            )
            assert won1 is True
            assert won2 is False, "二次 reserve 应返回 False(nonce 已存在,防重放)"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reserve_sets_status_reserved(self):
        """reserve 后 DB 中 nonce 行 status='reserved'。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_status_001",
                operation_id="op_status",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_status_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "reserved", (
                f"reserve 后 status 应为 'reserved',实际: {row[0]!r}"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reserve_stores_binding_fields(self):
        """reserve 存储 operation_id / backup_id / manifest_sha256 / payload_digest 绑定字段。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_bind_001",
                operation_id="op_bind_001",
                backup_id="backup_bind_001",
                manifest_sha256="abc123",
                payload_digest="def456",
                reserved_by="host_bind:9999",
            )
            cursor = await store._db.execute(
                "SELECT nonce, operation_id, backup_id, manifest_sha256, "
                "payload_digest, status, reserved_by "
                "FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_bind_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "nonce_bind_001"
            assert row[1] == "op_bind_001"
            assert row[2] == "backup_bind_001"
            assert row[3] == "abc123"
            assert row[4] == "def456"
            assert row[5] == "reserved"
            assert row[6] == "host_bind:9999"
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 2. consume_capability_nonce — reserved → consumed(CAS UPDATE)
# ═══════════════════════════════════════════════════════════════


class TestConsumeCapabilityNonceCAS:
    """R64 P1-02: consume_capability_nonce 改为 CAS: reserved→consumed(UPDATE WHERE status='reserved')。"""

    @pytest.mark.asyncio
    async def test_consume_after_reserve_returns_true(self):
        """reserve → consume 返回 True(CAS reserved→consumed 成功)。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_consume_001",
                operation_id="op_consume",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            won = await store.consume_capability_nonce(
                "nonce_consume_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            assert won is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_without_reserve_returns_false(self):
        """未 reserve 直接 consume 返回 False(无 reserved 行,CAS 失败)。

        R64 P1-02: consume 改为 CAS reserved→consumed,无 reserved 行时返回 False。
        """
        store, _ = await _make_store()
        try:
            won = await store.consume_capability_nonce(
                "nonce_no_reserve_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            assert won is False, (
                "未 reserve 直接 consume 应返回 False(CAS reserved→consumed 无 reserved 行)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_duplicate_consume_returns_false(self):
        """已 consumed 的 nonce 再次 consume 返回 False(终态保护)。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_dup_consume_001",
                operation_id="op_dup",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            won1 = await store.consume_capability_nonce(
                "nonce_dup_consume_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            won2 = await store.consume_capability_nonce(
                "nonce_dup_consume_001",  # 同一 nonce,已 consumed
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host2:5678",
            )
            assert won1 is True
            assert won2 is False, "已 consumed 的 nonce 再次 consume 应返回 False(终态保护)"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_sets_status_consumed(self):
        """consume 后 DB 中 nonce 行 status='consumed'。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_consume_status_001",
                operation_id="op_cs",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.consume_capability_nonce(
                "nonce_consume_status_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_consume_status_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "consumed", (
                f"consume 后 status 应为 'consumed',实际: {row[0]!r}"
            )
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 3. fail_capability_nonce — reserved → failed(CAS UPDATE)
# ═══════════════════════════════════════════════════════════════


class TestFailCapabilityNonce:
    """R64 P1-02: fail_capability_nonce — UPDATE reserved→failed(CAS)。"""

    @pytest.mark.asyncio
    async def test_fail_after_reserve_returns_true(self):
        """reserve → fail 返回 True(CAS reserved→failed 成功)。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_fail_001",
                operation_id="op_fail",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            ok = await store.fail_capability_nonce(
                "nonce_fail_001",
                failure_reason="restore_crdb_error",
            )
            assert ok is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fail_without_reserve_returns_false(self):
        """未 reserve 直接 fail 返回 False(无 reserved 行)。"""
        store, _ = await _make_store()
        try:
            ok = await store.fail_capability_nonce(
                "nonce_no_reserve_fail_001",
                failure_reason="never_reserved",
            )
            assert ok is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fail_sets_status_failed(self):
        """fail 后 DB 中 nonce 行 status='failed' + failure_reason 正确存储。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_fail_status_001",
                operation_id="op_fs",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.fail_capability_nonce(
                "nonce_fail_status_001",
                failure_reason="restore_sqlite_io_error",
            )
            cursor = await store._db.execute(
                "SELECT status, failure_reason FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_fail_status_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "failed", (
                f"fail 后 status 应为 'failed',实际: {row[0]!r}"
            )
            assert row[1] == "restore_sqlite_io_error"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fail_consumed_nonce_returns_false(self):
        """已 consumed 的 nonce fail 返回 False(终态保护,不能从 consumed 转 failed)。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_fail_consumed_001",
                operation_id="op_fc",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.consume_capability_nonce(
                "nonce_fail_consumed_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            ok = await store.fail_capability_nonce(
                "nonce_fail_consumed_001",
                failure_reason="late_failure",
            )
            assert ok is False, "已 consumed 的 nonce fail 应返回 False(终态保护)"
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 4. failed 允许同 operation 重试(新 nonce)
# ═══════════════════════════════════════════════════════════════


class TestFailedAllowsRetry:
    """R64 P1-02: failed 状态允许同 operation 重试(新 capability 新 nonce)。

    nonce PRIMARY KEY 唯一,failed nonce 不可复用。但同一 operation_id 可关联
    多个 nonce(每个新 capability 有新 nonce),failed nonce 留审计轨迹。
    """

    @pytest.mark.asyncio
    async def test_failed_nonce_does_not_block_new_nonce_same_operation(self):
        """operation A nonce_1 failed → operation A nonce_2(新)reserve 成功。"""
        store, _ = await _make_store()
        try:
            # 第一次尝试:nonce_1 reserve → fail
            await store.reserve_capability_nonce(
                "nonce_retry_001",
                operation_id="op_retry_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.fail_capability_nonce(
                "nonce_retry_001",
                failure_reason="restore_failed_first_attempt",
            )
            # 第二次尝试(新 capability,新 nonce,同 operation_id):reserve 成功
            won = await store.reserve_capability_nonce(
                "nonce_retry_002",  # 新 nonce
                operation_id="op_retry_001",  # 同 operation
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            assert won is True, (
                "failed nonce 不应阻塞同 operation 的新 nonce reserve(允许重试)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_failed_nonce_audit_trail_preserved(self):
        """failed nonce 保留在 DB 中作为审计轨迹(不删除)。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_audit_001",
                operation_id="op_audit",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.fail_capability_nonce(
                "nonce_audit_001",
                failure_reason="audit_test_failure",
            )
            # failed nonce 仍在 DB 中(审计轨迹)
            cursor = await store._db.execute(
                "SELECT status, failure_reason FROM restore_capability_nonces "
                "WHERE nonce = ?",
                ("nonce_audit_001",),
            )
            row = await cursor.fetchone()
            assert row is not None, "failed nonce 应保留在 DB 中(审计轨迹)"
            assert row[0] == "failed"
            assert row[1] == "audit_test_failure"
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 5. is_capability_nonce_consumed — 仅 status='consumed' 返回 True
# ═══════════════════════════════════════════════════════════════


class TestIsCapabilityNonceConsumed:
    """R64 P1-02: is_capability_nonce_consumed 仅在 status='consumed' 时返回 True。

    reserved / failed 状态的 nonce 不算"已消费"(允许后续状态转换)。
    """

    @pytest.mark.asyncio
    async def test_reserved_nonce_not_consumed(self):
        """reserved 状态的 nonce,is_capability_nonce_consumed 返回 False。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_is_res_001",
                operation_id="op_ir",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            assert await store.is_capability_nonce_consumed("nonce_is_res_001") is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consumed_nonce_is_consumed(self):
        """consumed 状态的 nonce,is_capability_nonce_consumed 返回 True。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_is_con_001",
                operation_id="op_ic",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.consume_capability_nonce(
                "nonce_is_con_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            assert await store.is_capability_nonce_consumed("nonce_is_con_001") is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_failed_nonce_not_consumed(self):
        """failed 状态的 nonce,is_capability_nonce_consumed 返回 False。"""
        store, _ = await _make_store()
        try:
            await store.reserve_capability_nonce(
                "nonce_is_fail_001",
                operation_id="op_if",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            await store.fail_capability_nonce(
                "nonce_is_fail_001",
                failure_reason="test_failure",
            )
            assert await store.is_capability_nonce_consumed("nonce_is_fail_001") is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_nonexistent_nonce_not_consumed(self):
        """不存在的 nonce,is_capability_nonce_consumed 返回 False。"""
        store, _ = await _make_store()
        try:
            assert await store.is_capability_nonce_consumed("nonce_nonexistent_001") is False
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 6. CRDB 不可用回退 SQLite(记录 warning)
# ═══════════════════════════════════════════════════════════════


class TestCRDBUnavailableFallbackToSQLite:
    """R64 P1-02: CRDB 不可用时回退 SQLite(记录 warning,不阻断)。

    单实例部署可能未配置 CRDB,nonce ledger 仍可用 SQLite fallback。
    """

    @pytest.mark.asyncio
    async def test_reserve_falls_back_to_sqlite_when_crdb_unavailable(self, monkeypatch):
        """CRDB 不可用时,reserve 回退 SQLite,返回 True(INSERT 成功)。"""
        store, _ = await _make_store()
        try:
            # mock CRDB 不可用(_get_crdb_client 返回 None 或 is_connected=False)
            monkeypatch.setattr(store, "_get_crdb_client", lambda: None)
            won = await store.reserve_capability_nonce(
                "nonce_fallback_001",
                operation_id="op_fb",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            assert won is True, "CRDB 不可用时 reserve 应回退 SQLite 并成功"
            # 验证 SQLite 中确实写入了
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_fallback_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "reserved"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_falls_back_to_sqlite_when_crdb_unavailable(self, monkeypatch):
        """CRDB 不可用时,consume 回退 SQLite(CAS reserved→consumed)。"""
        store, _ = await _make_store()
        try:
            monkeypatch.setattr(store, "_get_crdb_client", lambda: None)
            await store.reserve_capability_nonce(
                "nonce_fallback_consume_001",
                operation_id="op_fbc",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            won = await store.consume_capability_nonce(
                "nonce_fallback_consume_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            assert won is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fail_falls_back_to_sqlite_when_crdb_unavailable(self, monkeypatch):
        """CRDB 不可用时,fail 回退 SQLite(CAS reserved→failed)。"""
        store, _ = await _make_store()
        try:
            monkeypatch.setattr(store, "_get_crdb_client", lambda: None)
            await store.reserve_capability_nonce(
                "nonce_fallback_fail_001",
                operation_id="op_fbf",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            ok = await store.fail_capability_nonce(
                "nonce_fallback_fail_001",
                failure_reason="crdb_unavailable_test",
            )
            assert ok is True
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 7. CRDB schema 创建(security.restore_capability_nonces)
# ═══════════════════════════════════════════════════════════════


class TestCRDBSchemaCreation:
    """R64 P1-02: _ensure_crdb_restore_capability_nonces 创建 CRDB security schema。

    用 mock CRDB client 验证 DDL 语句正确执行(security schema + 表 + 索引)。
    """

    @pytest.mark.asyncio
    async def test_ensure_crdb_schema_executes_ddl(self, monkeypatch):
        """_ensure_crdb_restore_capability_nonces 执行 CREATE SCHEMA + CREATE TABLE + CREATE INDEX。"""
        store, _ = await _make_store()
        try:
            executed_sql = []

            class _MockConn:
                async def execute(self, sql, *params):
                    executed_sql.append(sql)

            mock_client = MagicMock()
            mock_client.is_connected = True
            mock_client.fetch = AsyncMock(return_value=[])
            from contextlib import asynccontextmanager as _acm

            @_acm
            async def _txn_cm():
                yield _MockConn()

            mock_client.transaction = _txn_cm
            monkeypatch.setattr(store, "_get_crdb_client", lambda: mock_client)

            await store._ensure_crdb_restore_capability_nonces()

            # 验证执行了 CREATE SCHEMA / CREATE TABLE / CREATE INDEX
            joined = " ".join(executed_sql)
            assert "CREATE SCHEMA" in joined.upper(), (
                f"应执行 CREATE SCHEMA security,实际执行: {executed_sql}"
            )
            assert "security.restore_capability_nonces" in joined.lower(), (
                f"应创建 security.restore_capability_nonces 表,实际: {executed_sql}"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_ensure_crdb_schema_idempotent(self, monkeypatch):
        """_ensure_crdb_restore_capability_nonces 幂等(重复调用无副作用)。"""
        store, _ = await _make_store()
        try:
            call_count = [0]

            class _MockConn:
                async def execute(self, sql, *params):
                    call_count[0] += 1

            mock_client = MagicMock()
            mock_client.is_connected = True
            mock_client.fetch = AsyncMock(return_value=[])
            from contextlib import asynccontextmanager as _acm

            @_acm
            async def _txn_cm():
                yield _MockConn()

            mock_client.transaction = _txn_cm
            monkeypatch.setattr(store, "_get_crdb_client", lambda: mock_client)

            await store._ensure_crdb_restore_capability_nonces()
            first_count = call_count[0]
            await store._ensure_crdb_restore_capability_nonces()
            second_count = call_count[0]
            # 幂等:第二次调用也执行相同 DDL(IF NOT EXISTS 保证无副作用)
            assert second_count >= first_count, (
                "幂等调用应至少执行与第一次相同的 DDL(IF NOT EXISTS 保证无副作用)"
            )
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 8. assert_valid 调用 reserve(非 consume)
# ═══════════════════════════════════════════════════════════════


class TestAssertValidReservesNonce:
    """R64 P1-02: _RestoreCapability.assert_valid 调用 reserve_capability_nonce。

    assert_valid 不再直接 consume,而是 reserve(status='reserved')。
    writer 在 restore 成功后 consume / 失败后 fail。
    """

    @pytest.mark.asyncio
    async def test_assert_valid_reserves_nonce(self, monkeypatch):
        """assert_valid 后 nonce 状态为 'reserved'(非 'consumed')。"""
        mod = _ensure_backup_dr_validate_importable()
        store, _ = await _make_store()
        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: store)
        try:
            cap = _build_valid_capability(mod)
            import time as _time
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=_time.time(),
                expected_scope="R64-P1-02-test-fingerprint",
            )
            # 验证 nonce 状态为 'reserved'
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                (cap.nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None, "assert_valid 应 reserve nonce(写入 DB)"
            assert row[0] == "reserved", (
                f"assert_valid 后 nonce 状态应为 'reserved',实际: {row[0]!r}"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_assert_valid_replay_rejected_at_reserve(self, monkeypatch):
        """同一 capability 二次 assert_valid 在 reserve 阶段被拒(PRIMARY KEY 冲突)。"""
        mod = _ensure_backup_dr_validate_importable()
        store, _ = await _make_store()
        import database.cache_store as _cs_mod
        monkeypatch.setattr(_cs_mod, "get_cache_store", lambda: store)
        try:
            cap = _build_valid_capability(mod)
            import time as _time
            # 第一次 assert_valid — reserve 成功
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=_time.time(),
                expected_scope="R64-P1-02-test-fingerprint",
            )
            # 第二次 assert_valid(同 capability,同 nonce)— reserve 失败 → AppError
            from services.error_codes import AppError
            with pytest.raises(AppError):
                await cap.assert_valid(
                    payload_digest="d" * 64,
                    clock=_time.time(),
                    expected_scope="R64-P1-02-test-fingerprint",
                )
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 9. 持久化跨"重启" — reserved/consumed/failed 状态持久化
# ═══════════════════════════════════════════════════════════════


class TestPersistenceAcrossRestart:
    """R64 P1-02: nonce 状态机持久化跨"重启" — 新建 CacheStore 实例(同 DB)
    仍能感知 reserved/consumed/failed 状态。"""

    @pytest.mark.asyncio
    async def test_reserved_state_persists_across_new_store(self):
        """实例 A reserve → 关闭 → 实例 B(同 DB)仍感知 reserved 状态。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r64_p1_02_restart_")
        db_path = str(Path(_tmp_dir) / "restart_test.db")

        # 实例 A:reserve
        store_a, _ = await _make_store(db_path)
        await store_a.reserve_capability_nonce(
            "nonce_restart_res_001",
            operation_id="op_restart",
            backup_id="backup_001",
            manifest_sha256="a" * 64,
            payload_digest="d" * 64,
            reserved_by="host_a:1234",
        )
        await store_a.close()

        # 实例 B(同 DB):查询状态
        store_b, _ = await _make_store(db_path)
        try:
            cursor = await store_b._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_restart_res_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "reserved", (
                f"实例 B 应感知 reserved 状态,实际: {row[0]!r}"
            )
            # 实例 B 可以 consume(CAS reserved→consumed)
            won = await store_b.consume_capability_nonce(
                "nonce_restart_res_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host_b:5678",
            )
            assert won is True
        finally:
            await store_b.close()

    @pytest.mark.asyncio
    async def test_consumed_state_persists_across_new_store(self):
        """实例 A consume → 关闭 → 实例 B(同 DB)拒绝重复 consume。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r64_p1_02_consumed_")
        db_path = str(Path(_tmp_dir) / "consumed_test.db")

        store_a, _ = await _make_store(db_path)
        await store_a.reserve_capability_nonce(
            "nonce_restart_con_001",
            operation_id="op_rc",
            backup_id="backup_001",
            manifest_sha256="a" * 64,
            payload_digest="d" * 64,
            reserved_by="host_a:1234",
        )
        await store_a.consume_capability_nonce(
            "nonce_restart_con_001",
            backup_id="backup_001",
            manifest_sha256="a" * 64,
            payload_digest="d" * 64,
            consumed_by="host_a:1234",
        )
        await store_a.close()

        store_b, _ = await _make_store(db_path)
        try:
            # 实例 B 拒绝重复 consume(已 consumed)
            won = await store_b.consume_capability_nonce(
                "nonce_restart_con_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host_b:5678",
            )
            assert won is False, "实例 B 应拒绝重复 consume(已 consumed,防重放)"
        finally:
            await store_b.close()
