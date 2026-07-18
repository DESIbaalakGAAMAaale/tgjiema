"""R63 P1-01: capability nonce 持久化到 SQLite/CRDB,原子消费。

审计背景:
    Python 下划线不是访问控制,外部代码仍可 import 模块属性。真正安全性应来自
    完整密码/状态验证,而非"外部无法访问 sentinel"的注释。原 R62 P0-01 的
    ``_CONSUMED_NONCES`` 只是进程内 set;多实例、重启和 worker 切换后不保留。

修复:
    restore operation/nonce 在权威 SQLite/CRDB 表 ``restore_capability_nonces``
    中原子消费;唯一键绑定 backup、target environment、manifest digest、
    payload digest;重启与多实例测试必须拒绝重复 destructive restore。

测试覆盖:
    1. **持久化跨"重启"**:同一 DB 文件,新建 CacheStore 实例 → 已消费 nonce 仍被拒绝
    2. **重复拒绝**:同一 nonce 二次消费返回 False,assert_valid 抛 AppError
    3. **并发竞态**:两个 CacheStore 实例并发消费同一 nonce,仅一个返回 True
    4. **绑定字段**:nonce + backup_id + manifest_sha256 + payload_digest 作为审计键
    5. **多实例**:两个 CacheStore 实例共享 DB 文件,互相同步 nonce 状态
    6. **_CONSUMED_NONCES 已移除**:模块不再有进程内 set
    7. **assert_valid 跨实例防重放**:实例 A 消费 nonce 后,实例 B 拒绝同一 capability
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


def _build_valid_capability(
    mod,
    *,
    payload_digest: str = "d" * 64,
    backup_id: str = "backup_test_001",
    manifest_sha256: str = "a" * 64,
    schema_fingerprint: str = "R63-P1-01-test-fingerprint",
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


async def _make_store(db_path: str | None = None):
    """构造一个真实 CacheStore(指定 db_path 或临时文件)。

    用于 R63 P1-01 nonce 持久化测试 — 替代原进程内 _CONSUMED_NONCES set。
    """
    from database.cache_store import CacheStore
    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_persist_")
        db_path = str(Path(_tmp_dir) / "test_cache.db")
    store = CacheStore(db_path=db_path)
    await store.init()
    return store, db_path


# ═══════════════════════════════════════════════════════════════
# 1. _CONSUMED_NONCES 已移除
# ═══════════════════════════════════════════════════════════════


class TestConsumedNoncesSetRemoved:
    """R63 P1-01: 验证原进程内 _CONSUMED_NONCES set 已被移除。"""

    def test_consumed_nonces_set_no_longer_exists(self):
        """模块不再有 _CONSUMED_NONCES 属性(已迁移到 DB 持久化)。"""
        mod = _ensure_backup_dr_validate_importable()
        assert not hasattr(mod, "_CONSUMED_NONCES"), (
            "R63 P1-01: _CONSUMED_NONCES 进程内 set 应已移除,"
            "nonce 持久化到权威 SQLite/CRDB 表 restore_capability_nonces"
        )

    def test_restore_sentinel_still_exists(self):
        """R63 P0-02: sentinel 构造保护仍保留(与 nonce 持久化分离)。"""
        mod = _ensure_backup_dr_validate_importable()
        assert hasattr(mod, "_RESTORE_SENTINEL"), (
            "_RESTORE_SENTINEL 应保留(P0-02 构造保护,与 P1-01 nonce 持久化无关)"
        )


# ═══════════════════════════════════════════════════════════════
# 2. CacheStore.consume_capability_nonce 原子 CAS
# ═══════════════════════════════════════════════════════════════


class TestConsumeCapabilityNonceAtomicCAS:
    """R63 P1-01: CacheStore.consume_capability_nonce 原子消费(INSERT OR IGNORE CAS)。"""

    @pytest.mark.asyncio
    async def test_first_consume_returns_true(self):
        """首次消费返回 True(本调用方赢得竞态)。"""
        store, _ = await _make_store()
        try:
            won = await store.consume_capability_nonce(
                "nonce_first_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            assert won is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_second_consume_returns_false(self):
        """同一 nonce 二次消费返回 False(已被消费,重放或竞态失败)。"""
        store, _ = await _make_store()
        try:
            won1 = await store.consume_capability_nonce(
                "nonce_second_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            won2 = await store.consume_capability_nonce(
                "nonce_second_001",  # 同一 nonce
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host2:5678",
            )
            assert won1 is True
            assert won2 is False, "二次消费应返回 False(防重放)"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_is_capability_nonce_consumed_reflects_state(self):
        """is_capability_nonce_consumed 反映消费状态(不消费,仅查询)。"""
        store, _ = await _make_store()
        try:
            # 消费前 — 未被消费
            assert await store.is_capability_nonce_consumed("nonce_query_001") is False
            # 消费
            await store.consume_capability_nonce(
                "nonce_query_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            # 消费后 — 已被消费
            assert await store.is_capability_nonce_consumed("nonce_query_001") is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_binding_fields_stored_correctly(self):
        """R63 P1-01: 绑定字段(backup_id + manifest_sha256 + payload_digest)正确存储。"""
        store, _ = await _make_store()
        try:
            await store.consume_capability_nonce(
                "nonce_bind_001",
                backup_id="backup_bind_001",
                manifest_sha256="abc123",
                payload_digest="def456",
                consumed_by="host_bind:9999",
            )
            # 直接查询 DB 验证绑定字段
            cursor = await store._db.execute(
                "SELECT nonce, backup_id, manifest_sha256, payload_digest, consumed_by "
                "FROM restore_capability_nonces WHERE nonce = ?",
                ("nonce_bind_001",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "nonce_bind_001"
            assert row[1] == "backup_bind_001"
            assert row[2] == "abc123"
            assert row[3] == "def456"
            assert row[4] == "host_bind:9999"
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 3. 持久化跨"重启"(新建 CacheStore 实例共享 DB 文件)
# ═══════════════════════════════════════════════════════════════


class TestPersistenceAcrossRestart:
    """R63 P1-01: nonce 持久化跨"重启" — 新建 CacheStore 实例(同一 DB 文件)
    仍能感知之前消费的 nonce。"""

    @pytest.mark.asyncio
    async def test_nonce_persists_across_new_store_instance(self):
        """实例 A 消费 nonce → 关闭 → 实例 B(同 DB)仍拒绝同一 nonce。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_restart_")
        db_path = str(Path(_tmp_dir) / "restart_test.db")

        # 实例 A:消费 nonce
        store_a, _ = await _make_store(db_path)
        won_a = await store_a.consume_capability_nonce(
            "nonce_restart_001",
            backup_id="backup_restart_001",
            manifest_sha256="a" * 64,
            payload_digest="d" * 64,
            consumed_by="host_a:1234",
        )
        assert won_a is True
        await store_a.close()

        # 实例 B:同一 DB 文件,新建 CacheStore(模拟重启)
        store_b, _ = await _make_store(db_path)
        try:
            # 实例 B 应感知到 nonce 已被消费
            is_consumed = await store_b.is_capability_nonce_consumed("nonce_restart_001")
            assert is_consumed is True, (
                "重启后(新 CacheStore 实例)应仍能感知 nonce 已被消费"
            )
            # 二次消费应失败
            won_b = await store_b.consume_capability_nonce(
                "nonce_restart_001",
                backup_id="backup_restart_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host_b:5678",
            )
            assert won_b is False, "重启后二次消费同一 nonce 应失败"
        finally:
            await store_b.close()

    @pytest.mark.asyncio
    async def test_assert_valid_rejects_after_restart(self):
        """实例 A 通过 assert_valid 消费 nonce → 重启 → 实例 B 拒绝同一 capability。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_assert_restart_")
        db_path = str(Path(_tmp_dir) / "assert_restart.db")

        # 实例 A:capability 通过 assert_valid(nonce 被消费)
        store_a, _ = await _make_store(db_path)
        cap = _build_valid_capability(mod, payload_digest="d" * 64)
        await cap.assert_valid(
            payload_digest="d" * 64,
            clock=time.time(),
            expected_scope="R63-P1-01-test-fingerprint",
            store=store_a,
        )
        await store_a.close()

        # 重启:新建 CacheStore 实例(同 DB)
        store_b, _ = await _make_store(db_path)
        try:
            # 同一 capability 再次 assert_valid — 应抛 AppError(防重放)
            with pytest.raises(AppError) as exc_info:
                await cap.assert_valid(
                    payload_digest="d" * 64,
                    clock=time.time(),
                    expected_scope="R63-P1-01-test-fingerprint",
                    store=store_b,
                )
            assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        finally:
            await store_b.close()


# ═══════════════════════════════════════════════════════════════
# 4. 多实例并发竞态(两个 CacheStore 实例共享 DB 文件)
# ═══════════════════════════════════════════════════════════════


class TestMultiInstanceConcurrentRace:
    """R63 P1-01: 两个 CacheStore 实例(WAL 模式)并发消费同一 nonce,
    仅一个返回 True(原子 CAS)。"""

    @pytest.mark.asyncio
    async def test_two_instances_concurrent_consume_only_one_wins(self):
        """两个 CacheStore 实例并发 consume 同一 nonce → 仅一个 True。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_race_")
        db_path = str(Path(_tmp_dir) / "race_test.db")

        # 预热:创建 DB schema
        store_init, _ = await _make_store(db_path)
        await store_init.close()

        # 两个独立 CacheStore 实例共享同一 DB 文件
        store_a, _ = await _make_store(db_path)
        store_b, _ = await _make_store(db_path)

        try:
            # 并发 consume 同一 nonce
            results = await asyncio.gather(
                store_a.consume_capability_nonce(
                    "nonce_race_001",
                    backup_id="backup_race",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="host_a:1234",
                ),
                store_b.consume_capability_nonce(
                    "nonce_race_001",
                    backup_id="backup_race",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="host_b:5678",
                ),
            )
            # 仅一个返回 True(原子 CAS)
            assert results.count(True) == 1, (
                f"并发消费同一 nonce 应仅一个 True,实际: {results}"
            )
            assert results.count(False) == 1
        finally:
            await store_a.close()
            await store_b.close()

    @pytest.mark.asyncio
    async def test_two_instances_distinct_nonces_both_win(self):
        """两个 CacheStore 实例并发 consume 不同 nonce → 都返回 True。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_distinct_")
        db_path = str(Path(_tmp_dir) / "distinct_test.db")

        store_init, _ = await _make_store(db_path)
        await store_init.close()

        store_a, _ = await _make_store(db_path)
        store_b, _ = await _make_store(db_path)

        try:
            results = await asyncio.gather(
                store_a.consume_capability_nonce(
                    "nonce_distinct_a",
                    backup_id="backup_distinct",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="host_a:1234",
                ),
                store_b.consume_capability_nonce(
                    "nonce_distinct_b",
                    backup_id="backup_distinct",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="host_b:5678",
                ),
            )
            assert results == [True, True], (
                "不同 nonce 并发消费应都成功(无冲突)"
            )
        finally:
            await store_a.close()
            await store_b.close()

    @pytest.mark.asyncio
    async def test_multi_instance_state_synchronized(self):
        """R63 P1-01 多实例:实例 A 消费 nonce 后,实例 B 立即感知(WAL 同步)。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_sync_")
        db_path = str(Path(_tmp_dir) / "sync_test.db")

        store_init, _ = await _make_store(db_path)
        await store_init.close()

        store_a, _ = await _make_store(db_path)
        store_b, _ = await _make_store(db_path)

        try:
            # 实例 A 消费
            won_a = await store_a.consume_capability_nonce(
                "nonce_sync_001",
                backup_id="backup_sync",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host_a:1234",
            )
            assert won_a is True

            # 实例 B 立即查询 — 应感知到 nonce 已被消费
            is_consumed_b = await store_b.is_capability_nonce_consumed("nonce_sync_001")
            assert is_consumed_b is True, (
                "WAL 模式下,实例 A 消费后实例 B 应立即感知"
            )
        finally:
            await store_a.close()
            await store_b.close()


# ═══════════════════════════════════════════════════════════════
# 5. assert_valid 集成测试 — 通过 store= 参数注入
# ═══════════════════════════════════════════════════════════════


class TestAssertValidWithStoreInjection:
    """R63 P1-01: assert_valid 通过 store= 参数注入 CacheStore,
    完成 nonce 原子消费。"""

    @pytest.mark.asyncio
    async def test_assert_valid_consumes_nonce_via_store(self):
        """assert_valid(store=...) 成功消费 nonce → 二次调用抛 AppError。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        store, _ = await _make_store()
        try:
            cap = _build_valid_capability(mod, payload_digest="d" * 64)
            now = time.time()
            # 第一次 — 成功
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=now,
                expected_scope="R63-P1-01-test-fingerprint",
                store=store,
            )
            # 第二次 — 抛 AppError(防重放)
            with pytest.raises(AppError) as exc_info:
                await cap.assert_valid(
                    payload_digest="d" * 64,
                    clock=now,
                    expected_scope="R63-P1-01-test-fingerprint",
                    store=store,
                )
            assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_assert_valid_two_caps_same_store_both_succeed(self):
        """两个不同 capability(不同 nonce)在同一 store 上 assert_valid — 都成功。"""
        mod = _ensure_backup_dr_validate_importable()
        store, _ = await _make_store()
        try:
            cap_a = _build_valid_capability(
                mod, payload_digest="d" * 64, backup_id="backup_a"
            )
            cap_b = _build_valid_capability(
                mod, payload_digest="d" * 64, backup_id="backup_b"
            )
            now = time.time()
            # 两个不同 capability(不同 nonce)都应成功
            await cap_a.assert_valid(
                payload_digest="d" * 64,
                clock=now,
                expected_scope="R63-P1-01-test-fingerprint",
                store=store,
            )
            await cap_b.assert_valid(
                payload_digest="d" * 64,
                clock=now,
                expected_scope="R63-P1-01-test-fingerprint",
                store=store,
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_assert_valid_cross_store_replay_rejected(self):
        """R63 P1-01 跨实例防重放:capability 在 store_a 消费后,
        store_b(同 DB)也应拒绝同一 capability。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError, ErrorCodes

        _tmp_dir = tempfile.mkdtemp(prefix="r63_p1_01_cross_")
        db_path = str(Path(_tmp_dir) / "cross_store.db")

        # 实例 A:消费 nonce
        store_a, _ = await _make_store(db_path)
        cap = _build_valid_capability(mod, payload_digest="d" * 64)
        now = time.time()
        await cap.assert_valid(
            payload_digest="d" * 64,
            clock=now,
            expected_scope="R63-P1-01-test-fingerprint",
            store=store_a,
        )
        await store_a.close()

        # 实例 B:同 DB,不同 CacheStore 实例 — 应拒绝同一 capability
        store_b, _ = await _make_store(db_path)
        try:
            with pytest.raises(AppError) as exc_info:
                await cap.assert_valid(
                    payload_digest="d" * 64,
                    clock=now,
                    expected_scope="R63-P1-01-test-fingerprint",
                    store=store_b,
                )
            assert exc_info.value.code == ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED
        finally:
            await store_b.close()

    @pytest.mark.asyncio
    async def test_assert_valid_app_error_carries_reason_param(self):
        """R63 P1-01: AppError 携带 params={"reason": "nonce_already_consumed"}。"""
        mod = _ensure_backup_dr_validate_importable()
        from services.error_codes import AppError

        store, _ = await _make_store()
        try:
            cap = _build_valid_capability(mod, payload_digest="d" * 64)
            now = time.time()
            # 第一次消费
            await cap.assert_valid(
                payload_digest="d" * 64,
                clock=now,
                expected_scope="R63-P1-01-test-fingerprint",
                store=store,
            )
            # 第二次 — 应携带 reason 参数
            with pytest.raises(AppError) as exc_info:
                await cap.assert_valid(
                    payload_digest="d" * 64,
                    clock=now,
                    expected_scope="R63-P1-01-test-fingerprint",
                    store=store,
                )
            # AppError 应携带 params={"reason": "nonce_already_consumed"}
            assert exc_info.value.params is not None
            assert exc_info.value.params.get("reason") == "nonce_already_consumed", (
                "AppError 应携带 params={'reason': 'nonce_already_consumed'}"
            )
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 6. 表结构与索引验证
# ═══════════════════════════════════════════════════════════════


class TestRestoreCapabilityNoncesTableSchema:
    """R63 P1-01: restore_capability_nonces 表结构与索引验证。"""

    @pytest.mark.asyncio
    async def test_table_exists_after_init(self):
        """CacheStore.init() 后 restore_capability_nonces 表存在。"""
        store, _ = await _make_store()
        try:
            cursor = await store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='restore_capability_nonces'"
            )
            row = await cursor.fetchone()
            assert row is not None, "restore_capability_nonces 表应存在"
            assert row[0] == "restore_capability_nonces"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_index_on_backup_id_exists(self):
        """idx_restore_nonces_backup_id 索引存在(支持按 backup_id 审计查询)。"""
        store, _ = await _make_store()
        try:
            cursor = await store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_restore_nonces_backup_id'"
            )
            row = await cursor.fetchone()
            assert row is not None, "idx_restore_nonces_backup_id 索引应存在"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_table_columns(self):
        """表包含所有必需列:nonce, backup_id, manifest_sha256, payload_digest,
        consumed_at, consumed_by。"""
        store, _ = await _make_store()
        try:
            cursor = await store._db.execute("PRAGMA table_info(restore_capability_nonces)")
            rows = await cursor.fetchall()
            column_names = {row[1] for row in rows}
            expected = {
                "nonce", "backup_id", "manifest_sha256",
                "payload_digest", "consumed_at", "consumed_by",
            }
            assert expected.issubset(column_names), (
                f"表应包含所有必需列,缺失: {expected - column_names}"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_nonce_is_primary_key(self):
        """nonce 列为 PRIMARY KEY(原子 CAS 的基础)。"""
        store, _ = await _make_store()
        try:
            cursor = await store._db.execute("PRAGMA table_info(restore_capability_nonces)")
            rows = await cursor.fetchall()
            nonce_col = next((r for r in rows if r[1] == "nonce"), None)
            assert nonce_col is not None
            # pk 字段非 0 表示是 PRIMARY KEY(PRAGMA table_info 第 6 列)
            assert nonce_col[5] != 0, "nonce 列应为 PRIMARY KEY"
        finally:
            await store.close()
