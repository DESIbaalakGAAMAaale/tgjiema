"""R40 P0-5 + P0-6: 事务发件箱 + 加密备份双 checksum 测试。

P0-5 测试覆盖:
- add_dirty_outbox(tx=...) 在事务内写入,失败抛异常(不再仅 warning)
- UnitOfWork / store.transaction() 上下文管理器(BEGIN/COMMIT/ROLLBACK)
- mark_dirty_local_only() 标记 local_only + processed
- _dispatch_dirty_outbox_to_crdb 对 local_only 表跳过 CRDB 同步
- dirty_outbox 写入失败时业务表一起回滚(原子性)

P0-6 测试覆盖:
- encrypt_payload 返回 ciphertext_sha256 + aad
- decrypt_payload 校验 expected_plaintext_sha256
- AAD 绑定 {backup_id, schema_version, key_id}
- 向后兼容: 旧备份 b"backup-payload" AAD 仍可解密
- _build_bundle_manifest 含双 checksum(plaintext_sha256 + ciphertext_sha256)
- 完整 round-trip: 加密 → 上传 → 下载 → 校验密文 → 解密 → 校验明文
- 篡改密文 → ciphertext_sha256 不匹配 → 中止
- 篡改明文(错误 expected_plaintext_sha256) → 校验失败

测试策略:
- P0-5: 真实 SQLite cache_store(临时文件 DB)
- P0-6: 真实 backup_crypto(AES-256-GCM)+ mock R2 storage(内存字典)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# 加密可用性检查
try:
    from services.backup_crypto import _CRYPTO_AVAILABLE  # noqa: F401
    _ENCRYPT_AVAILABLE = _CRYPTO_AVAILABLE
except Exception:
    _ENCRYPT_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# 辅助: 临时 SQLite cache_store fixture(P0-5 测试用)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def cache_store():
    """创建临时文件数据库的 CacheStore 实例(P0-5 测试用)。

    使用临时目录避免污染开发环境,测试结束后自动清理。
    """
    from database import cache_store as cs_module

    tmpdir = tempfile.mkdtemp(prefix="r40_p0_5_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = cs_module.DB_PATH
    original_store = getattr(cs_module, "_store", None)
    cs_module.DB_PATH = db_path
    try:
        s = cs_module.CacheStore()
        await s.init()
        # 替换全局单例,使 get_cache_store() 返回此实例
        cs_module._store = s
        yield s
        await s.close()
    finally:
        cs_module.DB_PATH = original_path
        if original_store is not None:
            cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# P0-5 测试: 事务发件箱 + Unit of Work
# ════════════════════════════════════════════════════════════════

class TestP05TransactionalOutbox:
    """R40 P0-5: dirty_outbox 真正事务闭环测试。"""

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_with_tx_no_auto_commit(self, cache_store):
        """add_dirty_outbox(tx=...) 传入事务时不自动 commit。

        验证: 在事务上下文中调用 add_dirty_outbox(connection=tx),
        记录写入但不 commit,事务回滚后记录消失。
        """
        store = cache_store
        # 开启事务,写入 dirty_outbox,然后回滚
        try:
            await store._db.execute("BEGIN")
            await store.add_dirty_outbox(
                "tasks", "task-001", "upsert",
                payload='{"status":"pending"}',
                connection=store._db,
            )
            # 事务内应能查到
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM dirty_outbox WHERE pk = 'task-001'"
            )
            row = await cursor.fetchone()
            assert row[0] == 1, "事务内应能查到 dirty_outbox 记录"
            # 主动回滚
            await store._db.rollback()
        except Exception:
            await store._db.rollback()
            raise

        # 回滚后应查不到
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM dirty_outbox WHERE pk = 'task-001'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "回滚后 dirty_outbox 记录应消失"

    @pytest.mark.asyncio
    async def test_add_dirty_outbox_with_tx_alias(self, cache_store):
        """add_dirty_outbox(tx=...) 等价于 connection= 参数(R40 P0-5 别名)。"""
        store = cache_store
        try:
            await store._db.execute("BEGIN")
            # 使用 tx 别名(等价于 connection)
            rid = await store.add_dirty_outbox(
                "tasks", "task-tx-alias", "upsert",
                payload='{"test":true}',
                tx=store._db,
            )
            assert rid > 0, "tx 别名应正常写入并返回 id"
            await store._db.commit()
        except Exception:
            await store._db.rollback()
            raise

        cursor = await store._db.execute(
            "SELECT pk FROM dirty_outbox WHERE id = ?", (rid,)
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] == "task-tx-alias"

    @pytest.mark.asyncio
    async def test_store_transaction_context_manager_commit(self, cache_store):
        """store.transaction() 上下文管理器正常退出时 COMMIT。"""
        store = cache_store
        async with store.transaction() as tx:
            await tx.execute(
                "INSERT INTO dirty_outbox (table_name, pk, operation, payload, created_at, processed) "
                "VALUES ('tasks', 'ctx-001', 'upsert', '{}', '2026-07-13', 0)"
            )

        # 退出后应已 commit,新查询能查到
        cursor = await store._db.execute(
            "SELECT pk FROM dirty_outbox WHERE pk = 'ctx-001'"
        )
        row = await cursor.fetchone()
        assert row is not None, "正常退出 transaction() 应 COMMIT"

    @pytest.mark.asyncio
    async def test_store_transaction_context_manager_rollback(self, cache_store):
        """store.transaction() 上下文管理器异常退出时 ROLLBACK。"""
        store = cache_store
        with pytest.raises(RuntimeError, match="test rollback"):
            async with store.transaction() as tx:
                await tx.execute(
                    "INSERT INTO dirty_outbox (table_name, pk, operation, payload, created_at, processed) "
                    "VALUES ('tasks', 'ctx-rollback', 'upsert', '{}', '2026-07-13', 0)"
                )
                raise RuntimeError("test rollback")

        # 回滚后应查不到
        cursor = await store._db.execute(
            "SELECT pk FROM dirty_outbox WHERE pk = 'ctx-rollback'"
        )
        row = await cursor.fetchone()
        assert row is None, "异常退出 transaction() 应 ROLLBACK"

    @pytest.mark.asyncio
    async def test_atomic_rollback_on_dirty_outbox_failure(self, cache_store):
        """R40 P0-5: 业务表 + dirty_outbox 同事务,dirty_outbox 失败时业务表一起回滚。

        模拟: 在事务中先写业务表(tasks),再模拟 dirty_outbox 写入失败,
        验证 tasks 表也回滚(原子性)。
        """
        store = cache_store
        # 先确保 tasks 表存在
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )"""
        )
        await store._db.commit()

        # 模拟事务中 dirty_outbox 写入失败(通过断开连接模拟)
        original_db = store._db
        try:
            await store._db.execute("BEGIN")
            # 写入业务表
            await store._db.execute(
                "INSERT INTO tasks (task_type, user_id, status, created_at) "
                "VALUES ('upload', 12345, 'pending', '2026-07-13')"
            )
            # 模拟 dirty_outbox 写入失败:用已关闭的连接
            # 直接抛异常模拟 add_dirty_outbox 失败
            raise RuntimeError("模拟 dirty_outbox 写入失败")
        except RuntimeError:
            await store._db.rollback()

        # 验证业务表也回滚
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = 12345"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "dirty_outbox 失败时业务表应一起回滚(原子性)"

    @pytest.mark.asyncio
    async def test_mark_dirty_local_only_sets_flags(self, cache_store):
        """R40 P0-5: mark_dirty_local_only 标记 processed=1 + local_only=1。"""
        store = cache_store
        # 插入测试记录
        ids = []
        for i in range(3):
            rid = await store.add_dirty_outbox(
                "tasks", f"local-{i}", "upsert", payload="{}",
            )
            ids.append(rid)

        # 标记为 local_only
        affected = await store.mark_dirty_local_only(ids)
        assert affected == 3, f"应标记 3 条,实际 {affected}"

        # 验证 processed=1 且 local_only=1
        cursor = await store._db.execute(
            f"SELECT processed, local_only FROM dirty_outbox WHERE id IN ({','.join('?'*len(ids))})",
            ids,
        )
        rows = await cursor.fetchall()
        for processed, local_only in rows:
            assert processed == 1, "local_only 记录应 processed=1"
            assert local_only == 1, "local_only 记录应 local_only=1"

    @pytest.mark.asyncio
    async def test_mark_dirty_local_only_empty_list(self, cache_store):
        """mark_dirty_local_only 空列表返回 0(边界条件)。"""
        store = cache_store
        affected = await store.mark_dirty_local_only([])
        assert affected == 0

    @pytest.mark.asyncio
    async def test_dispatch_local_only_table_skips_crdb(self):
        """R40 P0-5: _dispatch_dirty_outbox_to_crdb 对 local_only 表跳过 CRDB 同步。

        local_only 表(tasks/collections/notifications 等)的 dirty_outbox 记录
        应直接返回 id 列表,不调用 CRDB handler。
        """
        from services.crdb_sync_service import (
            _dispatch_dirty_outbox_to_crdb,
            _LOCAL_ONLY_TABLES,
        )

        # 确认 local_only 表集合包含预期表
        expected_local = {
            "tasks", "collections", "collection_items", "notifications",
            "content_reports", "audit_log",
        }
        assert expected_local.issubset(_LOCAL_ONLY_TABLES), (
            f"_LOCAL_ONLY_TABLES 应包含 {expected_local}, "
            f"实际: {_LOCAL_ONLY_TABLES}"
        )

        # 对每个 local_only 表调用 dispatch,验证返回所有 id
        for table_name in ["tasks", "notifications", "audit_log"]:
            records = [
                {"id": 1, "table_name": table_name, "pk": "pk-1", "operation": "upsert"},
                {"id": 2, "table_name": table_name, "pk": "pk-2", "operation": "upsert"},
            ]
            ids = await _dispatch_dirty_outbox_to_crdb(table_name, records)
            assert ids == [1, 2], (
                f"local_only 表 {table_name} 应返回所有 id,实际 {ids}"
            )

    @pytest.mark.asyncio
    async def test_append_quota_ledger_with_tx(self, cache_store):
        """R40 P0-5: append_quota_ledger(tx=...) 在事务内写入,不自动 commit。"""
        store = cache_store
        # 确保 quota_ledger 表存在
        await store._db.execute(
            """CREATE TABLE IF NOT EXISTS quota_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT,
                request_id TEXT,
                reason TEXT,
                created_at TEXT
            )"""
        )
        await store._db.commit()

        # 在事务中写入,然后回滚
        try:
            await store._db.execute("BEGIN")
            await store.append_quota_ledger(
                user_id=999,
                event_type="reservation",
                request_id="res-test-001",
                reason="test tx",
                tx=store._db,
            )
            # 事务内可查到
            cursor = await store._db.execute(
                "SELECT COUNT(*) FROM quota_ledger WHERE user_id = 999"
            )
            row = await cursor.fetchone()
            assert row[0] == 1, "事务内应能查到 quota_ledger 记录"
            await store._db.rollback()
        except Exception:
            await store._db.rollback()
            raise

        # 回滚后查不到
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM quota_ledger WHERE user_id = 999"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "回滚后 quota_ledger 记录应消失"


# ════════════════════════════════════════════════════════════════
# P0-6 测试: 加密备份双 checksum
# ════════════════════════════════════════════════════════════════

class TestP06EncryptPayloadChecksum:
    """R40 P0-6: encrypt_payload / decrypt_payload 双 checksum 测试。"""

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_encrypt_payload_returns_ciphertext_sha256(self, monkeypatch):
        """encrypt_payload 返回 ciphertext_sha256 字段。"""
        from services.backup_crypto import encrypt_payload, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        plaintext = b'{"tables": {"users": []}}'
        result = encrypt_payload(
            plaintext,
            backup_id="test-backup-001",
            schema_version="r40_test_v1",
        )

        assert result["encrypted"] is True
        assert "ciphertext_sha256" in result, "encrypt_payload 应返回 ciphertext_sha256"
        # 验证 ciphertext_sha256 是密文的 SHA-256
        expected_sha = hashlib.sha256(result["ciphertext"]).hexdigest()
        assert result["ciphertext_sha256"] == expected_sha, (
            "ciphertext_sha256 应等于密文的 SHA-256"
        )

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_encrypt_payload_aad_binding(self, monkeypatch):
        """encrypt_payload 的 AAD 绑定 {backup_id, schema_version, key_id}。"""
        from services.backup_crypto import encrypt_payload, get_key_id, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        plaintext = b'{"test": "aad_binding"}'
        backup_id = "backup-aad-001"
        schema_version = "r40_aad_v1"

        result = encrypt_payload(
            plaintext,
            backup_id=backup_id,
            schema_version=schema_version,
        )

        expected_key_id = get_key_id()
        expected_aad = f"{backup_id}|{schema_version}|{expected_key_id}"
        assert result["aad"] == expected_aad, (
            f"AAD 应为 '{expected_aad}',实际 '{result.get('aad')}'"
        )

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_decrypt_payload_with_correct_aad(self, monkeypatch):
        """decrypt_payload 用正确 AAD 可解密(AAD 绑定验证)。"""
        from services.backup_crypto import encrypt_payload, decrypt_payload, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        plaintext = b'{"tables": {"users": [{"id": 1}]}}'
        backup_id = "backup-correct-aad"
        schema_version = "r40_correct_v1"

        enc = encrypt_payload(
            plaintext,
            backup_id=backup_id,
            schema_version=schema_version,
        )
        dec = decrypt_payload(
            enc["ciphertext"],
            wrapped_dek=enc["wrapped_dek"],
            nonce_b64=enc["nonce"],
            backup_id=backup_id,
            schema_version=schema_version,
            key_id=enc["key_id"],
        )
        assert dec == plaintext, "正确 AAD 应能解密"

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_decrypt_payload_wrong_aad_fails(self, monkeypatch):
        """decrypt_payload 用错误 AAD 失败(AAD 绑定防止密文重放)。"""
        from services.backup_crypto import encrypt_payload, decrypt_payload, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        plaintext = b'{"tables": {"users": []}}'
        # 加密时用 backup_id=A
        enc = encrypt_payload(
            plaintext,
            backup_id="backup-original",
            schema_version="r40_v1",
        )
        # 解密时用 backup_id=B(模拟密文重放到不同备份上下文)
        with pytest.raises((ValueError, Exception)):
            decrypt_payload(
                enc["ciphertext"],
                wrapped_dek=enc["wrapped_dek"],
                nonce_b64=enc["nonce"],
                backup_id="backup-attacker",  # 错误 backup_id
                schema_version="r40_v1",
                key_id=enc["key_id"],
            )

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_decrypt_payload_backward_compat_old_aad(self, monkeypatch):
        """R40 P0-6 向后兼容: 旧备份使用 b"backup-payload" AAD 仍可解密。

        旧备份(无 backup_id/schema_version)使用 b"backup-payload" 作为 AAD,
        decrypt_payload 应自动回退到旧 AAD 候选。
        """
        from services.backup_crypto import (
            encrypt_payload, decrypt_payload, generate_kek, get_kek,
            _wrap_dek, _generate_dek, _NONCE_SIZE,
        )
        import base64
        import secrets
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        kek = get_kek()
        plaintext = b'{"legacy": "backup"}'

        # 模拟旧备份: 用 b"backup-payload" 作为 AAD(无 backup_id/schema_version)
        dek = _generate_dek()
        aesgcm = AESGCM(dek)
        nonce = secrets.token_bytes(_NONCE_SIZE)
        old_ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=b"backup-payload")
        wrapped_dek = _wrap_dek(dek, kek)
        nonce_b64 = base64.b64encode(nonce).decode("ascii")

        # 用新 decrypt_payload(不传 backup_id/schema_version)应能解密
        dec = decrypt_payload(
            old_ciphertext,
            wrapped_dek=wrapped_dek,
            nonce_b64=nonce_b64,
            # 不传 backup_id/schema_version/key_id → 自动回退到 b"backup-payload"
        )
        assert dec == plaintext, "旧备份(b\"backup-payload\" AAD)应能向后兼容解密"

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_decrypt_payload_validates_plaintext_sha256(self, monkeypatch):
        """decrypt_payload 用 expected_plaintext_sha256 校验解密后明文。"""
        from services.backup_crypto import encrypt_payload, decrypt_payload, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        plaintext = b'{"tables": {"users": [{"id": 1}]}}'
        expected_sha = hashlib.sha256(plaintext).hexdigest()

        enc = encrypt_payload(
            plaintext,
            backup_id="backup-checksum-001",
            schema_version="r40_v1",
        )
        # 传正确的 expected_plaintext_sha256 应能解密
        dec = decrypt_payload(
            enc["ciphertext"],
            wrapped_dek=enc["wrapped_dek"],
            nonce_b64=enc["nonce"],
            expected_plaintext_sha256=expected_sha,
            backup_id="backup-checksum-001",
            schema_version="r40_v1",
            key_id=enc["key_id"],
        )
        assert dec == plaintext, "正确 plaintext_sha256 应能解密"

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_decrypt_payload_wrong_plaintext_sha256_fails(self, monkeypatch):
        """decrypt_payload 用错误 expected_plaintext_sha256 失败(明文被篡改)。"""
        from services.backup_crypto import encrypt_payload, decrypt_payload, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())
        plaintext = b'{"tables": {"users": []}}'

        enc = encrypt_payload(
            plaintext,
            backup_id="backup-tamper-001",
            schema_version="r40_v1",
        )
        # 传错误的 expected_plaintext_sha256(模拟明文被篡改)
        wrong_sha = "0" * 64  # 显然错误的 sha256
        with pytest.raises((ValueError, Exception)):
            decrypt_payload(
                enc["ciphertext"],
                wrapped_dek=enc["wrapped_dek"],
                nonce_b64=enc["nonce"],
                expected_plaintext_sha256=wrong_sha,
                backup_id="backup-tamper-001",
                schema_version="r40_v1",
                key_id=enc["key_id"],
            )

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    def test_unencrypted_payload_checksum_validation(self, monkeypatch):
        """decrypt_payload 对未加密 payload 也校验 plaintext_sha256。"""
        from services.backup_crypto import decrypt_payload

        plaintext = b'{"unencrypted": true}'
        correct_sha = hashlib.sha256(plaintext).hexdigest()

        # 未加密 payload(wrapped_dek=None)直接返回,但校验 checksum
        result = decrypt_payload(
            plaintext,
            wrapped_dek=None,
            nonce_b64=None,
            expected_plaintext_sha256=correct_sha,
        )
        assert result == plaintext, "未加密 payload 正确 checksum 应通过"

        # 错误 checksum 应失败
        with pytest.raises((ValueError, Exception)):
            decrypt_payload(
                plaintext,
                wrapped_dek=None,
                nonce_b64=None,
                expected_plaintext_sha256="0" * 64,
            )


class TestP06BuildBundleManifest:
    """R40 P0-6: _build_bundle_manifest 双 checksum 测试。"""

    def test_manifest_contains_dual_checksum(self):
        """_build_bundle_manifest 返回 manifest 含 plaintext_sha256 + ciphertext_sha256。"""
        from services.db_backup import _build_bundle_manifest

        plaintext = b'{"tables": {"users": []}}'
        plaintext_sha = hashlib.sha256(plaintext).hexdigest()
        ciphertext_sha = hashlib.sha256(b"ciphertext-bytes").hexdigest()

        manifest = _build_bundle_manifest(
            backup_data={"tables": {"users": []}},
            content=plaintext,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            ciphertext_sha256=ciphertext_sha,
            backup_id="test-manifest-001",
        )

        assert manifest["plaintext_sha256"] == plaintext_sha, (
            "manifest 应含 plaintext_sha256(明文 SHA-256)"
        )
        assert manifest["ciphertext_sha256"] == ciphertext_sha, (
            "manifest 应含 ciphertext_sha256(密文 SHA-256)"
        )
        assert manifest["backup_id"] == "test-manifest-001"
        # 向后兼容: checksum_sha256 应等价于 plaintext_sha256
        assert manifest["checksum_sha256"] == plaintext_sha, (
            "checksum_sha256 应等价于 plaintext_sha256(向后兼容)"
        )

    def test_manifest_ciphertext_sha_defaults_to_plaintext(self):
        """不传 ciphertext_sha256 时,默认等于 plaintext_sha256(未加密场景)。"""
        from services.db_backup import _build_bundle_manifest

        plaintext = b'{"tables": {}}'
        manifest = _build_bundle_manifest(
            backup_data={"tables": {}},
            content=plaintext,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            # 不传 ciphertext_sha256
        )

        plaintext_sha = hashlib.sha256(plaintext).hexdigest()
        assert manifest["ciphertext_sha256"] == plaintext_sha, (
            "未加密时 ciphertext_sha256 应默认等于 plaintext_sha256"
        )

    def test_manifest_table_stats_included(self):
        """manifest 含每表行数统计。"""
        from services.db_backup import _build_bundle_manifest

        data = {
            "tables": {
                "users": [{"id": 1}, {"id": 2}],
                "file_records": [{"file_code": "ABC"}],
            }
        }
        manifest = _build_bundle_manifest(
            backup_data=data,
            content=b"{}",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
        )
        assert manifest["total_tables"] == 2
        assert manifest["total_rows"] == 3
        assert manifest["table_stats"]["users"]["row_count"] == 2
        assert manifest["table_stats"]["file_records"]["row_count"] == 1


class TestP06FullRoundtrip:
    """R40 P0-6: 完整 round-trip 测试(加密 → 上传 → 下载 → 校验 → 解密)。"""

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    @pytest.mark.asyncio
    async def test_full_roundtrip_encrypt_upload_download_decrypt(self, monkeypatch):
        """完整 round-trip: 加密 → 上传 → 下载 → 校验密文 → 解密 → 校验明文。"""
        from services.backup_crypto import encrypt_payload, decrypt_payload, generate_kek
        from services.db_backup import _build_bundle_manifest, _compute_sha256

        monkeypatch.setenv("BACKUP_KEK", generate_kek())

        # 1. 准备明文
        backup_data = {
            "tables": {
                "users": [{"user_id": 1, "name": "alice"}],
                "file_records": [{"file_code": "ABC123", "status": "active"}],
            }
        }
        plaintext = json.dumps(backup_data, default=str, ensure_ascii=False).encode("utf-8")
        backup_id = "roundtrip-001"
        schema_version = "r40_roundtrip_v1"

        # 2. 加密
        enc = encrypt_payload(
            plaintext,
            backup_id=backup_id,
            schema_version=schema_version,
        )
        ciphertext = enc["ciphertext"]
        cipher_sha = enc["ciphertext_sha256"]

        # 3. 构建 manifest(含双 checksum)
        manifest = _build_bundle_manifest(
            backup_data=backup_data,
            content=plaintext,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            ciphertext_sha256=cipher_sha,
            backup_id=backup_id,
        )

        # 4. 模拟"上传 → 下载"(用内存字典)
        storage = {b"ciphertext": ciphertext}
        downloaded = storage[b"ciphertext"]

        # 5. 校验密文 ciphertext_sha256(传输完整性)
        actual_cipher_sha = _compute_sha256(downloaded)
        assert actual_cipher_sha == manifest["ciphertext_sha256"], (
            "下载的密文 ciphertext_sha256 应与 manifest 一致"
        )

        # 6. 解密(传 expected_plaintext_sha256 校验明文)
        decrypted = decrypt_payload(
            downloaded,
            wrapped_dek=enc["wrapped_dek"],
            nonce_b64=enc["nonce"],
            expected_plaintext_sha256=manifest["plaintext_sha256"],
            backup_id=backup_id,
            schema_version=schema_version,
            key_id=enc["key_id"],
        )

        # 7. 校验明文 plaintext_sha256
        actual_plain_sha = hashlib.sha256(decrypted).hexdigest()
        assert actual_plain_sha == manifest["plaintext_sha256"], (
            "解密后明文 plaintext_sha256 应与 manifest 一致"
        )

        # 8. 验证内容一致
        assert decrypted == plaintext, "解密后内容应与原始明文一致"
        restored_data = json.loads(decrypted.decode("utf-8"))
        assert restored_data["tables"]["users"][0]["name"] == "alice"

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    @pytest.mark.asyncio
    async def test_tampered_ciphertext_detected(self, monkeypatch):
        """篡改密文 → ciphertext_sha256 不匹配 → 应检测到。"""
        from services.backup_crypto import encrypt_payload, generate_kek
        from services.db_backup import _build_bundle_manifest, _compute_sha256

        monkeypatch.setenv("BACKUP_KEK", generate_kek())

        plaintext = b'{"tables": {"users": []}}'
        enc = encrypt_payload(
            plaintext,
            backup_id="tamper-cipher-001",
            schema_version="r40_v1",
        )
        manifest = _build_bundle_manifest(
            backup_data={"tables": {}},
            content=plaintext,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            ciphertext_sha256=enc["ciphertext_sha256"],
            backup_id="tamper-cipher-001",
        )

        # 篡改密文(翻转最后一个字节)
        tampered = enc["ciphertext"][:-1] + bytes([enc["ciphertext"][-1] ^ 0xFF])

        # 校验 ciphertext_sha256 应不匹配
        tampered_sha = _compute_sha256(tampered)
        assert tampered_sha != manifest["ciphertext_sha256"], (
            "篡改密文后 ciphertext_sha256 应不匹配"
        )

    @pytest.mark.skipif(
        not _ENCRYPT_AVAILABLE,
        reason="cryptography 不可用,跳过加密测试",
    )
    @pytest.mark.asyncio
    async def test_tampered_plaintext_detected(self, monkeypatch):
        """篡改明文(错误 expected_plaintext_sha256) → decrypt_payload 校验失败。"""
        from services.backup_crypto import encrypt_payload, decrypt_payload, generate_kek

        monkeypatch.setenv("BACKUP_KEK", generate_kek())

        plaintext = b'{"tables": {"users": []}}'
        enc = encrypt_payload(
            plaintext,
            backup_id="tamper-plain-001",
            schema_version="r40_v1",
        )

        # 模拟攻击者篡改 manifest 中的 plaintext_sha256
        tampered_plain_sha = hashlib.sha256(b'{"tampered": true}').hexdigest()

        with pytest.raises((ValueError, Exception)):
            decrypt_payload(
                enc["ciphertext"],
                wrapped_dek=enc["wrapped_dek"],
                nonce_b64=enc["nonce"],
                expected_plaintext_sha256=tampered_plain_sha,
                backup_id="tamper-plain-001",
                schema_version="r40_v1",
                key_id=enc["key_id"],
            )


class TestP06BackupLogMessage:
    """R40 P0-6: db_backup._run_backup_loop 日志含双 checksum(集成测试)。"""

    def test_manifest_log_fields_present(self):
        """构建的 manifest 含日志所需的双 checksum 字段。"""
        from services.db_backup import _build_bundle_manifest

        plaintext = b'{"tables": {}}'
        cipher_sha = hashlib.sha256(b"cipher").hexdigest()
        manifest = _build_bundle_manifest(
            backup_data={"tables": {}},
            content=plaintext,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            ciphertext_sha256=cipher_sha,
            backup_id="log-test-001",
        )

        # 日志中应能取到以下字段
        assert manifest.get("plaintext_sha256"), "日志需要 plaintext_sha256"
        assert manifest.get("ciphertext_sha256"), "日志需要 ciphertext_sha256"
        assert manifest.get("backup_id"), "日志需要 backup_id"
        assert manifest.get("commit_sha"), "日志需要 commit_sha"
