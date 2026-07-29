"""R76 P0-06 / O8: nonce 从 /tmp 文件 CAS 切换到数据库 UNIQUE 约束 CAS。

审计背景(R76 终审报告 P0-06):
    R74 P1-04 使用 ``/tmp/restore_nonce_store`` 文件 ``os.open(O_EXCL)`` 实现 nonce
    防重放 CAS。但容器重建、runner 迁移或节点切换后 ``/tmp`` 状态丢失,R75 要求的
    SQLite/CRDB 事务 CAS 并未实现 — 等效于"无防重放"。

    R76 P0-06 / O8 整改:
        - **删除** ``/tmp/restore_nonce_store`` 文件 CAS 逻辑
        - **删除** ``nonce_store_dir`` 参数
        - **新增** 009 migration:``idx_restore_nonces_nonce_digest`` UNIQUE INDEX
          (Partial UNIQUE INDEX,WHERE nonce_digest IS NOT NULL)
        - **新增** 5 个独立绑定字段:
            - nonce_digest       — sha256(nonce),UNIQUE INDEX 键
            - capability_digest  — sha256(canonical_json(capability_without_signature))
            - target_identity    — 恢复目标数据库 identity hash(独立来源)
            - run_id             — GitHub Actions run ID(独立来源)
            - run_attempt        — GitHub Actions run attempt(独立来源)
        - RestoreNonceStore.reserve/consume/fail 传递 5 个新字段给 CacheStore
        - CacheStore.reserve_capability_nonce: INSERT 同时写入 5 个新字段,
          UNIQUE(nonce_digest) 提供第二重 CAS
        - CacheStore.consume_capability_nonce: WHERE 子句加入 capability_digest
          / operation_id 比对(防换 capability 重放)

测试覆盖:
    1. **nonce_digest UNIQUE INDEX 防重放**:相同 nonce 二次 reserve 失败
    2. **capability_digest 绑定**:同 nonce 不同 capability_digest 的 consume 失败
    3. **target_identity / run_id / run_attempt 字段正确存储与查询**
    4. **端到端测试**:真实 CacheStore + RestoreNonceStore + verify_and_consume_capability
       路径(不 mock 数据库)
    5. **并发双消费**:两个并发 consume 只能成功一次
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 测试辅助 ──────────────────────────────────────────────────


async def _make_store(db_path: str | None = None):
    """构造一个真实 CacheStore(指定 db_path 或临时文件)。

    用于 R76 P0-06 nonce 数据库 CAS 测试。每次测试用全新 DB 文件,
    天然隔离,且能验证跨"重启"(新建 CacheStore 实例)的持久化。
    """
    from database.cache_store import CacheStore
    if db_path is None:
        _tmp_dir = tempfile.mkdtemp(prefix="r76_p0_06_test_")
        db_path = str(Path(_tmp_dir) / "test_cache.db")
    store = CacheStore(db_path=db_path)
    await store.init()
    return store, db_path


def _make_operation_context(
    *,
    operation_id: str | None = None,
    backup_id: str = "backup_r76_001",
    source_sha: str = "a" * 40,
    run_id: int = 123456,
    run_attempt: int = 1,
    audience: str = "restore-writer",
    target_identity: str = "empty:sha256:" + "b" * 64,
    target_uri: str = "sqlite:///app/data/staging/cache_store.db",
    manifest_digest: str = "c" * 64,
    payload_digest: str = "d" * 64,
    allowed_action: str = "restore_to_blank_target",
    nonce: str = "",
):
    """构造一个 RestoreOperationContext(所有字段非空,通过 validate())。

    R76 P0-05: allowed_action / nonce 无默认值,必须显式传入。
    """
    from services.restore_operation_context import RestoreOperationContext
    return RestoreOperationContext(
        operation_id=operation_id or f"op_r76_{uuid.uuid4().hex[:8]}",
        backup_id=backup_id,
        source_sha=source_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        audience=audience,
        target_identity=target_identity,
        target_uri=target_uri,
        manifest_digest=manifest_digest,
        payload_digest=payload_digest,
        allowed_action=allowed_action,
        nonce=nonce,
    )


def _make_capability_dict(
    *,
    nonce: str,
    backup_id: str = "backup_r76_001",
    source_sha: str = "a" * 40,
    run_id: int = 123456,
    run_attempt: int = 1,
    audience: str = "restore-writer",
    target_database_identity: str = "empty:sha256:" + "b" * 64,
    target_uri: str = "sqlite:///app/data/staging/cache_store.db",
    target_path: str = "/app/data/staging/cache_store.db",
    operation_id: str | None = None,
    signature: str = "test_signature_placeholder",
):
    """构造一个 capability dict(不需要有效签名,仅供 RestoreNonceStore 使用)。

    RestoreNonceStore 只关心 nonce / capability_digest,不验证签名(签名由
    verify_capability 负责)。本测试聚焦 nonce CAS,故使用占位签名。
    """
    return {
        "schema_version": "1.0",
        "kind": "restore-capability",
        "operation_id": operation_id or f"op_r76_{uuid.uuid4().hex[:8]}",
        "backup_id": backup_id,
        "source_sha": source_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "audience": audience,
        "allowed_action": "restore_to_blank_target",
        "target_database_identity": target_database_identity,
        "target_path": target_path,
        "target_uri": target_uri,
        "issued_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "nonce": nonce,
        "key_id": "RESTORE_CAPABILITY_SIGNING_KEY",
        "signature": signature,
    }


def _compute_nonce_digest(nonce: str) -> str:
    """计算 nonce 的 SHA-256 摘要(与 RestoreNonceStore._compute_nonce_digest 一致)。"""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _compute_capability_digest(capability: dict) -> str:
    """计算 capability canonical JSON 的 SHA-256(排除 signature 字段)。"""
    cap_without_sig = {k: v for k, v in capability.items() if k != "signature"}
    canonical = json.dumps(
        cap_without_sig, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════
# 1. nonce_digest UNIQUE INDEX 防重放 — 相同 nonce 二次 reserve 失败
# ═══════════════════════════════════════════════════════════════


class TestNonceDigestUniqueIndexAntiReplay:
    """R76 P0-06: nonce_digest UNIQUE INDEX 实现数据库层 CAS 防重放。

    同一 nonce 二次 reserve 必须失败(UNIQUE 约束 + PRIMARY KEY 双重 CAS)。
    替代 R74 P1-04 的 /tmp 文件 os.open(O_EXCL) CAS。
    """

    @pytest.mark.asyncio
    async def test_same_nonce_second_reserve_fails_via_unique_index(self):
        """同 nonce 二次 reserve 返回 False(UNIQUE(nonce_digest) 冲突)。"""
        store, _ = await _make_store()
        try:
            nonce = "abc123def456abc123def456abc123de"
            nonce_digest = _compute_nonce_digest(nonce)
            # 第一次 reserve — 成功
            won1 = await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_unique_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=nonce_digest,
                capability_digest=_compute_capability_digest(
                    _make_capability_dict(nonce=nonce)
                ),
                target_identity="empty:sha256:" + "b" * 64,
                run_id=123456,
                run_attempt=1,
            )
            assert won1 is True, "首次 reserve 应成功"
            # 第二次 reserve(同 nonce_digest)— 失败(UNIQUE 冲突)
            won2 = await store.reserve_capability_nonce(
                nonce=nonce,  # 同一 nonce
                operation_id="op_unique_002",  # 不同 operation
                backup_id="backup_002",
                manifest_sha256="e" * 64,
                payload_digest="f" * 64,
                reserved_by="host2:5678",
                nonce_digest=nonce_digest,  # 同一 nonce_digest → UNIQUE 冲突
                capability_digest=_compute_capability_digest(
                    _make_capability_dict(nonce=nonce, backup_id="backup_002")
                ),
                target_identity="empty:sha256:" + "c" * 64,
                run_id=654321,
                run_attempt=2,
            )
            assert won2 is False, (
                "二次 reserve 同 nonce_digest 应失败(UNIQUE INDEX 防重放)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_different_nonce_with_null_digest_coexists(self):
        """nonce_digest=None 的老记录可共存(Partial UNIQUE INDEX 允许 NULL)。"""
        store, _ = await _make_store()
        try:
            # 两条 nonce_digest=None 的记录(Partial UNIQUE INDEX 允许多个 NULL)
            won1 = await store.reserve_capability_nonce(
                nonce="null_digest_nonce_001",
                operation_id="op_null_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                # 不传 nonce_digest 等新字段(向后兼容老调用方)
            )
            won2 = await store.reserve_capability_nonce(
                nonce="null_digest_nonce_002",
                operation_id="op_null_002",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            assert won1 is True
            assert won2 is True, (
                "两条 nonce_digest=NULL 的记录应可共存(Partial UNIQUE INDEX 允许 NULL)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_same_nonce_different_digest_both_succeed_but_index_blocks_replay(self):
        """同 nonce 不同 nonce_digest:PRIMARY KEY(nonce) 仍防重放(第二重 CAS)。"""
        store, _ = await _make_store()
        try:
            nonce = "primary_key_test_001"
            # 第一次 reserve — 成功
            won1 = await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_pk_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=_compute_nonce_digest(nonce),
                capability_digest="cap_digest_001",
                target_identity="target_001",
                run_id=1,
                run_attempt=1,
            )
            # 第二次 reserve(同 nonce,不同 nonce_digest)— PRIMARY KEY 冲突
            won2 = await store.reserve_capability_nonce(
                nonce=nonce,  # 同一 nonce → PRIMARY KEY 冲突
                operation_id="op_pk_002",
                backup_id="backup_002",
                manifest_sha256="e" * 64,
                payload_digest="f" * 64,
                reserved_by="host2:5678",
                nonce_digest="different_digest_value",  # 不同的 digest
                capability_digest="cap_digest_002",
                target_identity="target_002",
                run_id=2,
                run_attempt=2,
            )
            assert won1 is True
            assert won2 is False, (
                "同 nonce 不同 digest 仍应失败(PRIMARY KEY=nonce 第一重 CAS)"
            )
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 2. capability_digest 绑定 — 同 nonce 不同 capability_digest 的 consume 失败
# ═══════════════════════════════════════════════════════════════


class TestCapabilityDigestBinding:
    """R76 P0-06: consume 时 capability_digest 加入 WHERE 子句,防换 capability 重放。

    攻击场景:攻击者获取同 nonce 但篡改其他字段的 capability,
    试图用错误的 capability_digest 消费已 reserved 的 nonce。
    CAS WHERE 子句的 capability_digest 比对使该攻击失败。
    """

    @pytest.mark.asyncio
    async def test_consume_with_matching_capability_digest_succeeds(self):
        """consume 时 capability_digest 匹配 → 成功(正常路径)。"""
        store, _ = await _make_store()
        try:
            nonce = "cap_match_nonce_001"
            cap_digest = _compute_capability_digest(
                _make_capability_dict(nonce=nonce)
            )
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_cap_match_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=_compute_nonce_digest(nonce),
                capability_digest=cap_digest,
                target_identity="target_001",
                run_id=1,
                run_attempt=1,
            )
            won = await store.consume_capability_nonce(
                nonce=nonce,
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
                capability_digest=cap_digest,  # 匹配
                operation_id="op_cap_match_001",
            )
            assert won is True, "consume 时 capability_digest 匹配应成功"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_with_mismatched_capability_digest_fails(self):
        """consume 时 capability_digest 不匹配 → 失败(防换 capability 重放)。"""
        store, _ = await _make_store()
        try:
            nonce = "cap_mismatch_nonce_001"
            correct_digest = _compute_capability_digest(
                _make_capability_dict(nonce=nonce, backup_id="backup_001")
            )
            tampered_digest = _compute_capability_digest(
                _make_capability_dict(nonce=nonce, backup_id="backup_TAMPERED")
            )
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_cap_mismatch_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=_compute_nonce_digest(nonce),
                capability_digest=correct_digest,
                target_identity="target_001",
                run_id=1,
                run_attempt=1,
            )
            # 攻击者用篡改的 capability_digest 尝试 consume — 必须失败
            won = await store.consume_capability_nonce(
                nonce=nonce,
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="attacker:9999",
                capability_digest=tampered_digest,  # 不匹配
                operation_id="op_cap_mismatch_001",
            )
            assert won is False, (
                "consume 时 capability_digest 不匹配应失败(防换 capability 重放)"
            )
            # 验证 nonce 仍在 reserved 状态(未被攻击者消费)
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "reserved", (
                "capability_digest 不匹配时 nonce 应仍在 reserved 状态(未被消费)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_with_mismatched_operation_id_fails(self):
        """consume 时 operation_id 不匹配 → 失败(精确匹配预留行)。"""
        store, _ = await _make_store()
        try:
            nonce = "op_mismatch_nonce_001"
            cap_digest = _compute_capability_digest(
                _make_capability_dict(nonce=nonce)
            )
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_correct_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=_compute_nonce_digest(nonce),
                capability_digest=cap_digest,
                target_identity="target_001",
                run_id=1,
                run_attempt=1,
            )
            # 用错误的 operation_id 尝试 consume — 必须失败
            won = await store.consume_capability_nonce(
                nonce=nonce,
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="attacker:9999",
                capability_digest=cap_digest,
                operation_id="op_WRONG_001",  # 不匹配
            )
            assert won is False, (
                "consume 时 operation_id 不匹配应失败(精确匹配预留行)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_without_capability_digest_backward_compatible(self):
        """不传 capability_digest 时向后兼容(老调用方仍能消费)。"""
        store, _ = await _make_store()
        try:
            nonce = "backward_compat_nonce_001"
            # reserve 时不传新字段(老调用方)
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_bc_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
            )
            # consume 时也不传新字段(老调用方)
            won = await store.consume_capability_nonce(
                nonce=nonce,
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                consumed_by="host1:1234",
            )
            assert won is True, "不传新字段时 consume 应向后兼容成功"
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 3. target_identity / run_id / run_attempt 字段正确存储与查询
# ═══════════════════════════════════════════════════════════════


class TestR76BindingFieldsStorage:
    """R76 P0-06: 5 个独立绑定字段在 reserve 时正确存储到 DB。"""

    @pytest.mark.asyncio
    async def test_reserve_stores_all_r76_binding_fields(self):
        """reserve 后 DB 中 nonce_digest / capability_digest / target_identity /
        run_id / run_attempt 字段正确存储。"""
        store, _ = await _make_store()
        try:
            nonce = "field_storage_nonce_001"
            expected_nonce_digest = _compute_nonce_digest(nonce)
            cap_dict = _make_capability_dict(nonce=nonce)
            expected_cap_digest = _compute_capability_digest(cap_dict)
            expected_target = "empty:sha256:target_abc"
            expected_run_id = 987654
            expected_run_attempt = 3

            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_fields_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=expected_nonce_digest,
                capability_digest=expected_cap_digest,
                target_identity=expected_target,
                run_id=expected_run_id,
                run_attempt=expected_run_attempt,
            )
            cursor = await store._db.execute(
                "SELECT nonce_digest, capability_digest, target_identity, "
                "run_id, run_attempt "
                "FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None, "reserve 后应能查询到 nonce 行"
            assert row[0] == expected_nonce_digest, (
                f"nonce_digest 应为 {expected_nonce_digest},实际: {row[0]!r}"
            )
            assert row[1] == expected_cap_digest, (
                f"capability_digest 应为 {expected_cap_digest},实际: {row[1]!r}"
            )
            assert row[2] == expected_target, (
                f"target_identity 应为 {expected_target},实际: {row[2]!r}"
            )
            assert row[3] == expected_run_id, (
                f"run_id 应为 {expected_run_id},实际: {row[3]!r}"
            )
            assert row[4] == expected_run_attempt, (
                f"run_attempt 应为 {expected_run_attempt},实际: {row[4]!r}"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reserve_without_r76_fields_stores_null(self):
        """不传 R76 新字段时,DB 中对应字段为 NULL(向后兼容)。"""
        store, _ = await _make_store()
        try:
            nonce = "null_fields_nonce_001"
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_null_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                # 不传 nonce_digest / capability_digest / target_identity /
                # run_id / run_attempt
            )
            cursor = await store._db.execute(
                "SELECT nonce_digest, capability_digest, target_identity, "
                "run_id, run_attempt "
                "FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] is None, "未传 nonce_digest 时应为 NULL"
            assert row[1] is None, "未传 capability_digest 时应为 NULL"
            assert row[2] is None, "未传 target_identity 时应为 NULL"
            assert row[3] is None, "未传 run_id 时应为 NULL"
            assert row[4] is None, "未传 run_attempt 时应为 NULL"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_r76_fields_persist_across_new_store_instance(self):
        """R76 字段持久化跨"重启" — 新建 CacheStore 实例(同 DB)仍能查询。"""
        _tmp_dir = tempfile.mkdtemp(prefix="r76_p0_06_restart_")
        db_path = str(Path(_tmp_dir) / "restart_test.db")

        # 实例 A:reserve(写入 R76 字段)
        store_a, _ = await _make_store(db_path)
        nonce = "restart_persist_nonce_001"
        expected_nonce_digest = _compute_nonce_digest(nonce)
        expected_target = "empty:sha256:restart_target"
        expected_run_id = 111111
        expected_run_attempt = 7
        await store_a.reserve_capability_nonce(
            nonce=nonce,
            operation_id="op_restart_001",
            backup_id="backup_001",
            manifest_sha256="a" * 64,
            payload_digest="d" * 64,
            reserved_by="host_a:1234",
            nonce_digest=expected_nonce_digest,
            capability_digest="cap_digest_restart",
            target_identity=expected_target,
            run_id=expected_run_id,
            run_attempt=expected_run_attempt,
        )
        await store_a.close()

        # 实例 B(同 DB):查询 R76 字段
        store_b, _ = await _make_store(db_path)
        try:
            cursor = await store_b._db.execute(
                "SELECT nonce_digest, target_identity, run_id, run_attempt "
                "FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == expected_nonce_digest
            assert row[1] == expected_target
            assert row[2] == expected_run_id
            assert row[3] == expected_run_attempt
        finally:
            await store_b.close()


# ═══════════════════════════════════════════════════════════════
# 4. 端到端测试 — 真实 CacheStore + RestoreNonceStore + verify_and_consume_capability
# ═══════════════════════════════════════════════════════════════


class TestEndToEndVerifyAndConsumeCapability:
    """R76 P0-06: 端到端测试 — 真实 CacheStore + RestoreNonceStore +
    verify_and_consume_capability 路径(不 mock 数据库)。

    验证完整链路:
        1. issue_capability 签发 capability(含 HMAC 签名)
        2. RestoreOperationContext 构造(独立 expected 值)
        3. RestoreNonceStore.reserve(预留 nonce,写入 R76 字段)
        4. verify_and_consume_capability(校验 + 原子 consume)
        5. 二次 verify_and_consume_capability 失败(防重放)
    """

    @pytest.mark.asyncio
    async def test_e2e_reserve_then_verify_and_consume_succeeds(self):
        """端到端:reserve → verify_and_consume_capability 成功(完整链路)。"""
        from services.restore_capability_file import issue_capability, verify_and_consume_capability
        from services.restore_nonce_store import RestoreNonceStore
        import database.cache_store as _cs_mod

        store, _ = await _make_store()
        # mock get_cache_store 返回我们的测试 store
        original_get = getattr(_cs_mod, "get_cache_store", None)
        _cs_mod.get_cache_store = lambda: store
        try:
            signing_key = b"test_signing_key_r76_p0_06_e2e"
            nonce = "e2e_nonce_" + uuid.uuid4().hex[:16]
            operation_id = f"op_e2e_{uuid.uuid4().hex[:8]}"
            target_identity = "empty:sha256:e2e_target"

            # 1. 签发 capability(真实 HMAC 签名)
            capability = issue_capability(
                backup_id="backup_e2e_001",
                source_sha="e2e_source_sha_0000000000000000000000000000000000000000",
                target_database_identity=target_identity,
                target_path="/app/data/staging/cache_store.db",
                operation_id=operation_id,
                run_id=222222,
                run_attempt=1,
                audience="restore-writer",
                target_uri="sqlite:///app/data/staging/cache_store.db",
                ttl_seconds=3600,
                signing_key=signing_key,
                nonce=nonce,
            )

            # 2. 构造 RestoreOperationContext(独立 expected 值,与 capability 一致)
            context = _make_operation_context(
                operation_id=operation_id,
                backup_id="backup_e2e_001",
                source_sha="e2e_source_sha_0000000000000000000000000000000000000000",
                run_id=222222,
                run_attempt=1,
                audience="restore-writer",
                target_identity=target_identity,
                target_uri="sqlite:///app/data/staging/cache_store.db",
                manifest_digest="a" * 64,
                payload_digest="d" * 64,
                nonce=nonce,
            )

            # 3. RestoreNonceStore.reserve(预留 nonce)
            nonce_store = RestoreNonceStore(store)
            reserved = await nonce_store.reserve(
                capability, context, reserved_by="e2e_test:pid"
            )
            assert reserved is True, "reserve 应成功(nonce 之前不存在)"

            # 4. verify_and_consume_capability(校验 + 原子 consume)
            consumed = await verify_and_consume_capability(
                capability,
                signing_key=signing_key,
                operation_context=context,
                nonce_store=nonce_store,
            )
            assert consumed is True, "verify_and_consume_capability 应返回 True"

            # 5. 验证 DB 中 nonce 状态为 consumed
            cursor = await store._db.execute(
                "SELECT status, nonce_digest, capability_digest, target_identity, "
                "run_id, run_attempt "
                "FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "consumed", f"nonce 状态应为 consumed,实际: {row[0]!r}"
            # R76 字段正确存储
            assert row[1] == _compute_nonce_digest(nonce), "nonce_digest 应正确存储"
            assert row[2] is not None, "capability_digest 应非空"
            assert row[3] == target_identity, "target_identity 应正确存储"
            assert row[4] == 222222, "run_id 应正确存储"
            assert row[5] == 1, "run_attempt 应正确存储"
        finally:
            if original_get is not None:
                _cs_mod.get_cache_store = original_get
            await store.close()

    @pytest.mark.asyncio
    async def test_e2e_replay_after_consume_rejected(self):
        """端到端:consume 后再次 reserve 同 nonce 失败(防重放)。"""
        from services.restore_capability_file import issue_capability, verify_and_consume_capability
        from services.restore_nonce_store import RestoreNonceStore
        import database.cache_store as _cs_mod

        store, _ = await _make_store()
        original_get = getattr(_cs_mod, "get_cache_store", None)
        _cs_mod.get_cache_store = lambda: store
        try:
            signing_key = b"test_signing_key_r76_p0_06_replay"
            nonce = "replay_nonce_" + uuid.uuid4().hex[:16]
            operation_id = f"op_replay_{uuid.uuid4().hex[:8]}"
            target_identity = "empty:sha256:replay_target"

            capability = issue_capability(
                backup_id="backup_replay_001",
                source_sha="replay_sha_00000000000000000000000000000000000000000",
                target_database_identity=target_identity,
                target_path="/app/data/staging/cache_store.db",
                operation_id=operation_id,
                run_id=333333,
                run_attempt=1,
                audience="restore-writer",
                target_uri="sqlite:///app/data/staging/cache_store.db",
                ttl_seconds=3600,
                signing_key=signing_key,
                nonce=nonce,
            )

            context = _make_operation_context(
                operation_id=operation_id,
                backup_id="backup_replay_001",
                source_sha="replay_sha_00000000000000000000000000000000000000000",
                run_id=333333,
                run_attempt=1,
                audience="restore-writer",
                target_identity=target_identity,
                target_uri="sqlite:///app/data/staging/cache_store.db",
                manifest_digest="a" * 64,
                payload_digest="d" * 64,
                nonce=nonce,
            )

            nonce_store = RestoreNonceStore(store)
            # 第一次:reserve → consume 成功
            await nonce_store.reserve(capability, context, reserved_by="replay_test:1")
            await verify_and_consume_capability(
                capability,
                signing_key=signing_key,
                operation_context=context,
                nonce_store=nonce_store,
            )

            # 第二次:再次 reserve 同 nonce — 必须失败(UNIQUE(nonce_digest) 防重放)
            reserved_again = await nonce_store.reserve(
                capability, context, reserved_by="replay_test:2"
            )
            assert reserved_again is False, (
                "已 consumed 的 nonce 再次 reserve 应失败(防重放)"
            )
        finally:
            if original_get is not None:
                _cs_mod.get_cache_store = original_get
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 5. 并发双消费 — 两个并发 consume 只能成功一次
# ═══════════════════════════════════════════════════════════════


class TestConcurrentConsumeOnlyOneSucceeds:
    """R76 P0-06: 并发双消费场景 — CAS UPDATE 保证只能成功一次。

    模拟两个 worker 同时调用 consume_capability_nonce,
    数据库 CAS UPDATE 的 rowcount 保证只有一个返回 1(成功),另一个返回 0(失败)。
    """

    @pytest.mark.asyncio
    async def test_concurrent_consume_only_one_succeeds(self):
        """两个并发 consume 调用,只有一个返回 True(CAS 保证)。"""
        store, _ = await _make_store()
        try:
            nonce = "concurrent_nonce_001"
            cap_digest = _compute_capability_digest(
                _make_capability_dict(nonce=nonce)
            )
            # 先 reserve
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_concurrent_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=_compute_nonce_digest(nonce),
                capability_digest=cap_digest,
                target_identity="target_concurrent",
                run_id=444444,
                run_attempt=1,
            )

            # 两个并发 consume(同一 nonce,同一 capability_digest)
            # SQLite 的 UPDATE 是原子的,串行化执行,只有一个 rowcount==1
            results = await asyncio.gather(
                store.consume_capability_nonce(
                    nonce=nonce,
                    backup_id="backup_001",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="worker_A:1111",
                    capability_digest=cap_digest,
                    operation_id="op_concurrent_001",
                ),
                store.consume_capability_nonce(
                    nonce=nonce,
                    backup_id="backup_001",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="worker_B:2222",
                    capability_digest=cap_digest,
                    operation_id="op_concurrent_001",
                ),
            )
            # 只有一个成功,另一个失败
            success_count = sum(1 for r in results if r is True)
            assert success_count == 1, (
                f"并发 consume 应只有一个成功,实际: {success_count} 个成功"
                f"(results={results})"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_concurrent_consume_with_mismatched_digest_both_fail(self):
        """两个并发 consume 都用错误的 capability_digest — 都失败(nonce 仍 reserved)。"""
        store, _ = await _make_store()
        try:
            nonce = "concurrent_mismatch_nonce_001"
            correct_digest = _compute_capability_digest(
                _make_capability_dict(nonce=nonce, backup_id="backup_001")
            )
            wrong_digest_a = _compute_capability_digest(
                _make_capability_dict(nonce=nonce, backup_id="wrong_A")
            )
            wrong_digest_b = _compute_capability_digest(
                _make_capability_dict(nonce=nonce, backup_id="wrong_B")
            )
            await store.reserve_capability_nonce(
                nonce=nonce,
                operation_id="op_conc_mm_001",
                backup_id="backup_001",
                manifest_sha256="a" * 64,
                payload_digest="d" * 64,
                reserved_by="host1:1234",
                nonce_digest=_compute_nonce_digest(nonce),
                capability_digest=correct_digest,
                target_identity="target_mm",
                run_id=555555,
                run_attempt=1,
            )

            results = await asyncio.gather(
                store.consume_capability_nonce(
                    nonce=nonce,
                    backup_id="backup_001",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="attacker_A:1111",
                    capability_digest=wrong_digest_a,  # 错误 digest
                    operation_id="op_conc_mm_001",
                ),
                store.consume_capability_nonce(
                    nonce=nonce,
                    backup_id="backup_001",
                    manifest_sha256="a" * 64,
                    payload_digest="d" * 64,
                    consumed_by="attacker_B:2222",
                    capability_digest=wrong_digest_b,  # 错误 digest
                    operation_id="op_conc_mm_001",
                ),
            )
            # 两个都失败(都用错误的 capability_digest)
            assert results == [False, False], (
                f"两个并发 consume 用错误 capability_digest 都应失败,实际: {results}"
            )
            # nonce 仍在 reserved 状态
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "reserved", (
                "并发失败后 nonce 应仍在 reserved 状态(未被消费)"
            )
        finally:
            await store.close()


# ═══════════════════════════════════════════════════════════════
# 6. RestoreNonceStore 集成测试 — reserve/consume/fail 传递 R76 字段
# ═══════════════════════════════════════════════════════════════


class TestRestoreNonceStoreR76Integration:
    """R76 P0-06: RestoreNonceStore.reserve/consume/fail 正确传递 R76 字段给 CacheStore。"""

    @pytest.mark.asyncio
    async def test_reserve_via_store_writes_r76_fields(self):
        """RestoreNonceStore.reserve 写入 R76 字段到 DB。"""
        from services.restore_nonce_store import RestoreNonceStore
        store, _ = await _make_store()
        try:
            nonce = "store_reserve_nonce_001"
            capability = _make_capability_dict(nonce=nonce)
            context = _make_operation_context(nonce=nonce)

            nonce_store = RestoreNonceStore(store)
            reserved = await nonce_store.reserve(
                capability, context, reserved_by="store_test:1"
            )
            assert reserved is True

            # 验证 R76 字段写入 DB
            cursor = await store._db.execute(
                "SELECT nonce_digest, capability_digest, target_identity, "
                "run_id, run_attempt "
                "FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == _compute_nonce_digest(nonce), "nonce_digest 应写入"
            assert row[1] == _compute_capability_digest(capability), (
                "capability_digest 应写入"
            )
            assert row[2] == context.target_identity, "target_identity 应写入"
            assert row[3] == context.run_id, "run_id 应写入"
            assert row[4] == context.run_attempt, "run_attempt 应写入"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_via_store_with_capability_digest_cas(self):
        """RestoreNonceStore.consume 通过 capability_digest CAS 消费。"""
        from services.restore_nonce_store import RestoreNonceStore
        store, _ = await _make_store()
        try:
            nonce = "store_consume_nonce_001"
            capability = _make_capability_dict(nonce=nonce)
            context = _make_operation_context(nonce=nonce)

            nonce_store = RestoreNonceStore(store)
            await nonce_store.reserve(capability, context, reserved_by="store_test:2")
            consumed = await nonce_store.consume(
                capability, context, consumed_by="store_test:2"
            )
            assert consumed is True, "consume 应成功(capability_digest 匹配)"

            # 验证 nonce 状态为 consumed
            cursor = await store._db.execute(
                "SELECT status FROM restore_capability_nonces WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "consumed"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_consume_via_store_with_tampered_capability_fails(self):
        """RestoreNonceStore.consume 用篡改的 capability 失败(防换 capability 重放)。"""
        from services.restore_nonce_store import RestoreNonceStore
        store, _ = await _make_store()
        try:
            nonce = "store_tamper_nonce_001"
            # 原始 capability(reserve 时使用)
            original_cap = _make_capability_dict(
                nonce=nonce, backup_id="backup_original"
            )
            context = _make_operation_context(nonce=nonce, backup_id="backup_original")

            nonce_store = RestoreNonceStore(store)
            await nonce_store.reserve(original_cap, context, reserved_by="store_test:3")

            # 篡改的 capability(同 nonce,不同 backup_id → 不同 capability_digest)
            tampered_cap = _make_capability_dict(
                nonce=nonce, backup_id="backup_TAMPERED"
            )
            consumed = await nonce_store.consume(
                tampered_cap, context, consumed_by="attacker:9999"
            )
            assert consumed is False, (
                "用篡改的 capability consume 应失败(capability_digest 不匹配)"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fail_via_store_passes_audit_fields(self):
        """RestoreNonceStore.fail 传递 operation_id / capability_digest(审计字段)。"""
        from services.restore_nonce_store import RestoreNonceStore
        store, _ = await _make_store()
        try:
            nonce = "store_fail_nonce_001"
            capability = _make_capability_dict(nonce=nonce)
            context = _make_operation_context(nonce=nonce)

            nonce_store = RestoreNonceStore(store)
            await nonce_store.reserve(capability, context, reserved_by="store_test:4")
            failed = await nonce_store.fail(
                capability, context, failure_reason="restore_crdb_error"
            )
            assert failed is True, "fail 应成功(reserved→failed)"

            # 验证 nonce 状态为 failed
            cursor = await store._db.execute(
                "SELECT status, failure_reason FROM restore_capability_nonces "
                "WHERE nonce = ?",
                (nonce,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "failed"
            assert row[1] == "restore_crdb_error"
        finally:
            await store.close()
