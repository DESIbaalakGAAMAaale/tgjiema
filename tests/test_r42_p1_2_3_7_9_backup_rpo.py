"""R42 P1-2 / P1-3 / P1-7 / P1-9: 备份引擎 + 孤儿 GC + 跨对象原子提交 +
生产恢复审批 + 逐表 Backup/Restore policy + 真实 RPO/RTO 监控。

测试覆盖:
    P1-2 跨对象原子提交 + 孤儿 GC:
        - cleanup_orphans 孤儿识别(无 COMPLETE marker)
        - cleanup_orphans 超时删除
        - cleanup_orphans 未超时不删除(正在进行的备份保护)
        - cleanup_orphans 写 audit_log
        - enable_object_lock 占位
        - backup_gc.run_backup_gc 返回统计
        - run_backup_gc_job 便利函数

    P1-3 生产恢复审批标识:
        - production restore 无 approval_action_id 抛 ValueError
        - approval_action_id 不存在抛 PermissionError
        - approval 未审批(status != 'executed')抛 PermissionError
        - approver_id 不一致抛 PermissionError
        - 通过审批后执行恢复
        - target="test" 不需要 approval

    P1-7 逐表 Backup/Restore policy:
        - BACKUP_POLICY 各表分类正确
        - get_backup_policy 未知表返回 LOCAL_ONLY
        - _validate_backup_replication_consistency 无冲突通过
        - _validate_backup_replication_consistency 有冲突抛 ValueError
        - backup() 对 MUST_RESTORE 完整备份
        - backup() 对 NO_EXPORT_PLAINTEXT 数据为 <<REDACTED>>
        - backup() 对 REBUILDABLE 仅备份 schema(数据清空)
        - backup() 对 LOCAL_ONLY 不备份
        - restore() 对各类 policy 的恢复策略

    P1-9 RPO/RTO 真实 COMPLETE 状态:
        - get_last_successful_backup 无备份返回 None
        - get_last_successful_backup 有备份返回 dict
        - get_last_successful_backup COMPLETE marker 缺失返回 None
        - 无备份时 tgjiema_backup_compliant=0
        - 有成功备份时 tgjiema_backup_compliant=1
        - RPO 计算:基于 last_backup_at

测试策略:
    - 复用 R41 P1-5 测试的 mock 模式(_FakeR2Storage / _FakeCacheStore)
    - 不依赖真实 R2 / CRDB / SQLite(全 mock)
    - 中文注释
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.error_codes import AppError, ErrorCodes

# 测试文件顶部 mock telegram 模块(避免 import 失败)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# 备份加密可用性检查
try:
    from services.backup_crypto import _CRYPTO_AVAILABLE  # noqa: F401
    _ENCRYPT_AVAILABLE = _CRYPTO_AVAILABLE
except Exception:
    _ENCRYPT_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# 辅助: mock storage / cache_store / backup_all_tables
# ════════════════════════════════════════════════════════════════


class _FakeR2Storage:
    """模拟 R2 storage:用内存字典存储所有上传的对象。

    支持 upload/download/delete/list_objects,并允许"破坏"对象
    以测试 checksum 不匹配场景。
    """

    def __init__(self):
        self._objects: dict[str, bytes] = {}
        # 显式 last_modified 映射(若未设置,默认使用当前时间)
        self._last_modified: dict[str, str] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "") -> str:
        self._objects[key] = bytes(data)
        # 默认 last_modified = 当前时间
        if key not in self._last_modified:
            self._last_modified[key] = datetime.now(timezone.utc).isoformat()
        return key

    async def download(self, key: str) -> bytes:
        if key not in self._objects:
            raise KeyError(f"R2 object not found: {key}")
        return self._objects[key]

    async def delete(self, key: str):
        self._objects.pop(key, None)
        self._last_modified.pop(key, None)

    async def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict]:
        result = []
        for key, data in self._objects.items():
            if key.startswith(prefix):
                result.append({
                    "key": key,
                    "size": len(data),
                    "last_modified": self._last_modified.get(key, ""),
                })
            if len(result) >= max_keys:
                break
        return result

    def _set_last_modified(self, key: str, iso_ts: str) -> None:
        """显式设置对象的 last_modified(用于测试超时判断)。"""
        self._last_modified[key] = iso_ts

    def _corrupt(self, key: str) -> None:
        """人为篡改已上传对象的内容。"""
        if key in self._objects:
            original = self._objects[key]
            tampered = original[:-1] + bytes([original[-1] ^ 0xFF]) if original else b"\x00"
            self._objects[key] = tampered

    def _remove(self, key: str) -> None:
        """人为删除已上传对象。"""
        self._objects.pop(key, None)
        self._last_modified.pop(key, None)


class _FakeCacheStore:
    """模拟 cache_store:提供 get_kv/set_kv 接口,可选 _db 用于审批校验。"""

    def __init__(self):
        self._kv: dict[str, str] = {}
        # _db 用于 mock 审批校验(_validate_production_approval 中读取 command_executions)
        self._db = None

    async def get_kv(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set_kv(self, key: str, value: str):
        self._kv[key] = value


class _FakeCursor:
    """模拟 aiosqlite.Cursor:返回预置的 fetchone 结果。"""

    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetchone(self):
        if self._rows:
            return self._rows[0]
        return None

    async def fetchall(self):
        return list(self._rows)


class _FakeDB:
    """模拟 aiosqlite.Connection:返回预置的查询结果。

    支持:
        - set_query_result(pattern, rows):匹配 pattern 的查询返回 rows
        - set_raise_on(pattern, exc):匹配 pattern 的查询/commit 抛 exc
          (用于测试异常降级路径,如 audit_log 表不存在时降级到 kv_store)
    """

    def __init__(self):
        # query_pattern → rows
        self._query_results: dict[str, list] = {}
        # raise_pattern → exception(匹配时抛出)
        self._raise_patterns: dict[str, Exception] = {}
        self._last_query: str = ""

    def set_query_result(self, query_substring: str, rows: list) -> None:
        """设置匹配查询子串的返回结果。

        Args:
            query_substring: SQL 语句子串(用于模糊匹配)
            rows: 返回的行列表,每行是 tuple
        """
        self._query_results[query_substring] = rows

    def set_raise_on(self, query_substring: str, exc: Exception | None = None) -> None:
        """设置匹配查询子串时抛出异常(用于测试降级路径)。

        Args:
            query_substring: SQL 语句子串(用于模糊匹配)
            exc: 抛出的异常(默认 sqlite3.OperationalError 模拟表不存在)
        """
        if exc is None:
            import sqlite3
            exc = sqlite3.OperationalError(f"no such table: {query_substring}")
        self._raise_patterns[query_substring] = exc

    async def execute(self, query: str, params=None):
        self._last_query = query
        # 优先检查 raise 模式
        for pattern, exc in self._raise_patterns.items():
            if pattern in query:
                raise exc
        for pattern, rows in self._query_results.items():
            if pattern in query:
                return _FakeCursor(rows)
        # 默认返回空结果
        return _FakeCursor([])

    async def commit(self):
        # commit 也检查 raise 模式(若需测试 commit 失败)
        for pattern, exc in self._raise_patterns.items():
            if pattern.lower() == "commit":
                raise exc
        pass


def _build_engine_with_kek(monkeypatch, kek_b64: str | None = None):
    """构造一个注入 mock storage/cache_store 的 BackupEngine,并设置 BACKUP_KEK。"""
    from services.backup_engine import BackupEngine
    from services.backup_crypto import generate_kek

    if kek_b64 is None:
        kek_b64 = generate_kek()
    monkeypatch.setenv("BACKUP_KEK", kek_b64)
    monkeypatch.delenv("BACKUP_KEK_PREVIOUS", raising=False)
    monkeypatch.delenv("BACKUP_KEK_PREVIOUS_FILE", raising=False)

    fake_storage = _FakeR2Storage()
    fake_cache = _FakeCacheStore()
    engine = BackupEngine(storage=fake_storage, cache_store=fake_cache)
    return engine, fake_storage, fake_cache, kek_b64


def _patch_backup_all_tables(monkeypatch, tables: dict | None = None):
    """Mock services.db_backup.backup_all_tables 返回固定 backup_data。"""
    if tables is None:
        tables = {
            "users": [{"user_id": 1, "name": "alice"}, {"user_id": 2, "name": "bob"}],
            "file_records": [{"file_code": "ABC123", "status": "active"}],
        }

    async def _fake_backup_all_tables(watermark=None, backup_type="full"):
        return {
            "backup_time": "2026-07-13T10:00:00",
            "tables": tables,
            "_r38_p1_5_metadata": {
                "start_time": "2026-07-13T10:00:00",
                "end_time": "2026-07-13T10:00:01",
                "backup_type": backup_type,
                "watermark": None,
                "prev_watermark": None,
            },
        }

    monkeypatch.setattr(
        "services.db_backup.backup_all_tables", _fake_backup_all_tables,
    )
    return tables


# ════════════════════════════════════════════════════════════════
# P1-2: 跨对象原子提交 + 孤儿 GC
# ════════════════════════════════════════════════════════════════


class TestP12CleanupOrphans:
    """R42 P1-2: BackupEngine.cleanup_orphans 孤儿对象清理。"""

    @pytest.mark.asyncio
    async def test_cleanup_orphans_identifies_orphan_without_complete(self, monkeypatch):
        """孤儿识别:有 payload + manifest 但无 COMPLETE marker 的备份应被识别。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 模拟一个孤儿对象(payload + manifest,无 COMPLETE marker)
        orphan_id = "backup_20260101_000000_orphan01"
        await storage.upload(f"backups/{orphan_id}.enc", b"fake_payload")
        await storage.upload(
            f"backups/{orphan_id}.manifest.json", b"{}",
        )
        # 设置为 2 小时前(超过默认 1 小时阈值)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        storage._set_last_modified(f"backups/{orphan_id}.enc", old_ts)
        storage._set_last_modified(f"backups/{orphan_id}.manifest.json", old_ts)

        # 调用 cleanup_orphans(timeout=3600 = 1 小时)
        result = await engine.cleanup_orphans(timeout_seconds=3600)

        assert result["scanned"] >= 1
        assert result["deleted"] >= 2  # payload + manifest
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_orphans_skips_complete_backups(self, monkeypatch):
        """完整备份(有 COMPLETE marker)不应被清理。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 创建一个完整备份
        manifest = await engine.create_backup(backup_type="full")
        complete_id = manifest["backup_id"]

        # 调用 cleanup_orphans
        result = await engine.cleanup_orphans(timeout_seconds=0)

        # 完整备份的三个对象都应保留
        assert f"backups/{complete_id}.enc" in storage._objects
        assert f"backups/{complete_id}.manifest.json" in storage._objects
        assert f"backups/{complete_id}.complete" in storage._objects
        # 不应有删除
        assert result["deleted"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_orphans_skips_recent_orphans(self, monkeypatch):
        """未超时的孤儿不应被清理(保护正在进行的备份)。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 模拟一个刚创建的孤儿(1 分钟前)
        orphan_id = "backup_recent_orphan_0001"
        await storage.upload(f"backups/{orphan_id}.enc", b"fake_payload")
        await storage.upload(f"backups/{orphan_id}.manifest.json", b"{}")
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        storage._set_last_modified(f"backups/{orphan_id}.enc", recent_ts)
        storage._set_last_modified(f"backups/{orphan_id}.manifest.json", recent_ts)

        # timeout=3600 秒(1 小时),1 分钟前的孤儿不应被清理
        result = await engine.cleanup_orphans(timeout_seconds=3600)

        # 孤儿应被识别但未删除
        assert result["scanned"] >= 1
        assert result["deleted"] == 0  # 未超时,不删除

    @pytest.mark.asyncio
    async def test_cleanup_orphans_deletes_expired_orphans(self, monkeypatch):
        """超时的孤儿应被删除。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 模拟一个超时的孤儿(2 小时前)
        orphan_id = "backup_expired_orphan_0001"
        await storage.upload(f"backups/{orphan_id}.enc", b"fake_payload")
        await storage.upload(f"backups/{orphan_id}.manifest.json", b"{}")
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        storage._set_last_modified(f"backups/{orphan_id}.enc", old_ts)
        storage._set_last_modified(f"backups/{orphan_id}.manifest.json", old_ts)

        # timeout=3600 秒(1 小时),2 小时前的孤儿应被清理
        result = await engine.cleanup_orphans(timeout_seconds=3600)

        assert result["deleted"] >= 2  # payload + manifest
        # 对象应已被删除
        assert f"backups/{orphan_id}.enc" not in storage._objects
        assert f"backups/{orphan_id}.manifest.json" not in storage._objects

    @pytest.mark.asyncio
    async def test_cleanup_orphans_writes_audit_log(self, monkeypatch):
        """cleanup_orphans 应写 audit_log(若 cache_store 支持)。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 模拟一个超时孤儿
        orphan_id = "backup_audit_orphan_0001"
        await storage.upload(f"backups/{orphan_id}.enc", b"fake_payload")
        await storage.upload(f"backups/{orphan_id}.manifest.json", b"{}")
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        storage._set_last_modified(f"backups/{orphan_id}.enc", old_ts)
        storage._set_last_modified(f"backups/{orphan_id}.manifest.json", old_ts)

        # mock _db 用于 audit_log 写入
        # 让 INSERT INTO audit_log 抛异常(模拟表不存在),触发 kv_store 降级路径
        fake_db = _FakeDB()
        fake_db.set_raise_on("INSERT INTO audit_log")
        cache._db = fake_db

        # 调用 cleanup_orphans
        await engine.cleanup_orphans(timeout_seconds=3600)

        # 验证 audit_log 降级到 kv_store.backup_gc_audit_log
        # (因为 INSERT INTO audit_log 抛异常,触发降级路径)
        audit_log_raw = await cache.get_kv("backup_gc_audit_log")
        # 应有 audit log 记录(降级到 kv_store)
        assert audit_log_raw is not None, "cleanup_orphans 应写 audit_log"
        audit_entries = json.loads(audit_log_raw)
        assert isinstance(audit_entries, list)
        assert len(audit_entries) >= 1
        # 最新一条应记录 cleanup 操作
        latest = audit_entries[-1]
        assert latest["action"] == "backup_gc_cleanup_orphans"
        assert "scanned" in latest
        assert "deleted" in latest
        assert "errors" in latest

    @pytest.mark.asyncio
    async def test_cleanup_orphans_returns_stats_dict(self, monkeypatch):
        """cleanup_orphans 返回 dict 应包含 scanned/deleted/errors/details 字段。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        result = await engine.cleanup_orphans(timeout_seconds=3600)

        assert isinstance(result, dict)
        assert "scanned" in result
        assert "deleted" in result
        assert "errors" in result
        assert "details" in result
        assert isinstance(result["scanned"], int)
        assert isinstance(result["deleted"], int)
        assert isinstance(result["errors"], int)

    @pytest.mark.asyncio
    async def test_cleanup_orphans_handles_list_objects_failure(self, monkeypatch):
        """list_objects 失败时返回 errors=0 但 details 提示失败。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # mock list_objects 抛异常
        async def _failing_list(prefix="", max_keys=1000):
            raise RuntimeError("R2 list 失败")
        storage.list_objects = _failing_list

        result = await engine.cleanup_orphans(timeout_seconds=3600)

        assert result["scanned"] == 0
        assert result["deleted"] == 0
        assert "list_objects 失败" in result["details"]

    @pytest.mark.asyncio
    async def test_enable_object_lock_returns_placeholder(self, monkeypatch):
        """enable_object_lock 占位实现应返回 enabled=False 与说明。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        result = await engine.enable_object_lock(
            bucket_name="tgjiema-backups", retention_days=30,
        )

        assert isinstance(result, dict)
        assert result["enabled"] is False  # 占位始终返回 False
        assert result["bucket_name"] == "tgjiema-backups"
        assert result["retention_days"] == 30
        assert "details" in result
        assert "Object Lock" in result["details"] or "R2" in result["details"]


# ════════════════════════════════════════════════════════════════
# P1-2: backup_gc.run_backup_gc / run_backup_gc_job
# ════════════════════════════════════════════════════════════════


class TestP12BackupGcModule:
    """R42 P1-2: services/backup_gc.py 模块测试。"""

    @pytest.mark.asyncio
    async def test_run_backup_gc_returns_stats(self, monkeypatch):
        """run_backup_gc 应返回包含 scanned/deleted/errors 的 dict。"""
        from services import backup_gc
        from services.backup_engine import BackupEngine

        # 构造 BackupEngine + 注入 mock storage/cache
        fake_storage = _FakeR2Storage()
        fake_cache = _FakeCacheStore()
        # 不直接构造 engine,而是 mock BackupEngine.__new__ 来返回我们准备的实例
        # 实际上,我们用 monkeypatch 替换 BackupEngine.cleanup_orphans
        # 让它直接返回我们预置的统计

        async def _fake_cleanup_orphans(self_, timeout_seconds=3600):
            return {
                "scanned": 5, "deleted": 3, "errors": 0,
                "details": "test stats",
            }

        monkeypatch.setattr(
            "services.backup_engine.BackupEngine.cleanup_orphans",
            _fake_cleanup_orphans,
        )

        stats = await backup_gc.run_backup_gc(timeout_seconds=3600)
        assert stats["scanned"] == 5
        assert stats["deleted"] == 3
        assert stats["errors"] == 0

    @pytest.mark.asyncio
    async def test_run_backup_gc_handles_exception(self, monkeypatch):
        """run_backup_gc 在异常时应返回 errors=1 而非抛异常。"""
        from services import backup_gc

        async def _failing_cleanup(self_, timeout_seconds=3600):
            raise RuntimeError("simulated failure")
        monkeypatch.setattr(
            "services.backup_engine.BackupEngine.cleanup_orphans",
            _failing_cleanup,
        )

        stats = await backup_gc.run_backup_gc(timeout_seconds=3600)
        assert stats["scanned"] == 0
        assert stats["deleted"] == 0
        assert stats["errors"] == 1
        assert "异常" in stats["details"] or "simulated" in stats["details"]

    @pytest.mark.asyncio
    async def test_run_backup_gc_job_does_not_raise(self, monkeypatch):
        """run_backup_gc_job 应吞掉异常,不传播给调用方。"""
        from services import backup_gc

        async def _failing_cleanup(self_, timeout_seconds=3600):
            raise RuntimeError("simulated failure")
        monkeypatch.setattr(
            "services.backup_engine.BackupEngine.cleanup_orphans",
            _failing_cleanup,
        )

        # 不应抛异常
        await backup_gc.run_backup_gc_job()

    @pytest.mark.asyncio
    async def test_run_backup_gc_job_returns_none(self, monkeypatch):
        """run_backup_gc_job 返回 None(便利函数,不返回统计)。"""
        from services import backup_gc

        async def _ok_cleanup(self_, timeout_seconds=3600):
            return {"scanned": 1, "deleted": 0, "errors": 0, "details": "ok"}
        monkeypatch.setattr(
            "services.backup_engine.BackupEngine.cleanup_orphans",
            _ok_cleanup,
        )

        result = await backup_gc.run_backup_gc_job()
        assert result is None

    @pytest.mark.asyncio
    async def test_run_backup_gc_passes_timeout_argument(self, monkeypatch):
        """run_backup_gc 应将 timeout_seconds 透传给 cleanup_orphans。"""
        from services import backup_gc

        captured = {"timeout": None}

        async def _capturing_cleanup(self_, timeout_seconds=3600):
            captured["timeout"] = timeout_seconds
            return {"scanned": 0, "deleted": 0, "errors": 0, "details": ""}

        monkeypatch.setattr(
            "services.backup_engine.BackupEngine.cleanup_orphans",
            _capturing_cleanup,
        )

        await backup_gc.run_backup_gc(timeout_seconds=7200)
        assert captured["timeout"] == 7200


# ════════════════════════════════════════════════════════════════
# P1-3: 生产恢复审批标识
# ════════════════════════════════════════════════════════════════


class TestP13ProductionRestoreApproval:
    """R42 P1-3: production restore 审批标识可伪造修复。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_without_approval_action_id_raises(self, monkeypatch):
        """production restore 无 approval_action_id 应抛 AppError(BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED)。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # production restore 必须提供 approval_action_id
        with pytest.raises(AppError) as exc_info:
            await engine.restore(
                backup_id, target="production", approver_id=999,
            )
        assert exc_info.value.envelope.code == ErrorCodes.BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_approval_not_found_raises(self, monkeypatch):
        """approval_action_id 在 command_executions 不存在 → PermissionError。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 配置 mock _db 返回 None(approval 不存在)
        fake_db = _FakeDB()
        fake_db.set_query_result("command_executions", [])  # fetchone 返回 None
        cache._db = fake_db

        # R51 P0-8: production restore 必须传 expected_request_hash
        with pytest.raises(PermissionError, match="不存在"):
            await engine.restore(
                backup_id, target="production", approver_id=999,
                approval_action_id="nonexistent_id",
                expected_request_hash="some_hash",
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_approval_not_executed_raises(self, monkeypatch):
        """approval_action_id status != 'approved' → PermissionError(R51 P0-8 状态语义)。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 配置 mock _db 返回 status='pending'(未审批)
        fake_db = _FakeDB()
        fake_db.set_query_result(
            "command_executions",
            [(999, "pending", None)],  # R44: (principal_id, status, request_hash)
        )
        cache._db = fake_db

        # R51 P0-8: production restore 必须传 expected_request_hash
        # R51 P0-8: status='pending' 非 'approved' → PermissionError "非 approved"
        with pytest.raises(PermissionError, match="非 approved"):
            await engine.restore(
                backup_id, target="production", approver_id=999,
                approval_action_id="approval_pending_id",
                expected_request_hash="some_hash",
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_approver_mismatch_raises(self, monkeypatch):
        """approver_id 与 principal_id 不一致 → PermissionError。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # mock _db 返回 principal_id=888,与 approver_id=999 不匹配
        # R51 P0-8: status='approved'(通过状态检查),hash 匹配(通过 hash 检查)
        # 但 approver_id=999 ≠ principal_id=888 → PermissionError "不一致"
        fake_db = _FakeDB()
        fake_db.set_query_result(
            "command_executions",
            [(888, "approved", "stored_hash")],  # R51: status=approved, request_hash=stored_hash
        )
        cache._db = fake_db

        with pytest.raises(PermissionError, match="不一致"):
            await engine.restore(
                backup_id, target="production", approver_id=999,
                approval_action_id="approval_mismatch_id",
                expected_request_hash="stored_hash",
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_production_restore_with_valid_approval_succeeds(self, monkeypatch):
        """approver_id 与 principal_id 一致 + status='approved' + hash 匹配 → 恢复成功。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # mock _db 返回匹配的审批记录
        # R51 P0-8: status='approved'(审批通过等待执行),request_hash 与传入值一致
        fake_db = _FakeDB()
        fake_db.set_query_result(
            "command_executions",
            [(999, "approved", "stored_hash")],  # R51: status=approved, request_hash=stored_hash
        )
        cache._db = fake_db

        # mock db_restore
        async def _fake_restore_from_backup_data(*args, **kwargs):
            return {"restored_tables": 2, "restored_rows": 3}
        monkeypatch.setattr(
            "services.db_restore._restore_from_backup_data",
            _fake_restore_from_backup_data,
        )
        monkeypatch.setattr(
            "services.restore_writer._restore_from_backup_data",
            _fake_restore_from_backup_data,
        )

        # R52 P0-5: mock claim_execution_approved 通过(CAS approved→executing)
        # _FakeDB 不支持 cursor.rowcount 和 execute_fetchall,需 mock CAS 绕过
        async def _noop_claim(action_id, owner, request_hash=None, lease_seconds=None):
            return True
        monkeypatch.setattr(
            "services.command_bus.claim_execution_approved", _noop_claim,
        )
        async def _noop_mark_executed(action_id, result=None):
            return True
        async def _noop_mark_failed(action_id, error="", retryable=False):
            return True
        monkeypatch.setattr(
            "services.command_bus.mark_approved_executed", _noop_mark_executed,
        )
        monkeypatch.setattr(
            "services.command_bus.mark_approved_failed", _noop_mark_failed,
        )

        result = await engine.restore(
            backup_id, target="production", approver_id=999,
            approval_action_id="approval_valid_id",
            expected_request_hash="stored_hash",
        )

        assert result["success"] is True
        assert result["checksum_verified"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_staging_restore_does_not_require_approval(self, monkeypatch):
        """target='staging' 不需要 approval_action_id。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # staging restore 不传 approval_action_id 应成功
        result = await engine.restore(
            backup_id, target="staging", approver_id=0,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_test_target_does_not_require_approval(self, monkeypatch):
        """target='test' 不需要 approval_action_id(只校验可解密)。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # test target 不传 approval_action_id 应成功
        result = await engine.restore(
            backup_id, target="test", approver_id=0,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_complete_marker_missing_returns_error(self, monkeypatch):
        """COMPLETE marker 缺失时 restore 应返回 success=False。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 删除 COMPLETE marker
        storage._remove(f"backups/{backup_id}.complete")

        result = await engine.restore(
            backup_id, target="staging", approver_id=0,
        )

        assert result["success"] is False
        assert "COMPLETE marker 缺失" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_restore_empty_backup_id_returns_error(self, monkeypatch):
        """backup_id 为空时 restore 应返回 success=False。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        result = await engine.restore("", target="staging", approver_id=0)
        assert result["success"] is False
        assert "backup_id 为空" in result["error"]


# ════════════════════════════════════════════════════════════════
# P1-7: 逐表 Backup/Restore policy
# ════════════════════════════════════════════════════════════════


class TestP17BackupPolicy:
    """R42 P1-7: 逐表 Backup/Restore Policy 校验。"""

    def test_backup_policy_must_restore_tables(self):
        """MUST_RESTORE 表应包含核心业务表(users/file_records/codes)。"""
        from services.backup_schema import BACKUP_POLICY, BackupPolicy

        must_restore_tables = [
            "users", "file_records", "codes", "cells",
            "rbac_roles", "rbac_user_roles", "approvals",
            "command_outbox", "command_executions",
            "audit_log", "ban_state", "collections",
        ]
        for table in must_restore_tables:
            assert BACKUP_POLICY.get(table) is BackupPolicy.MUST_RESTORE, \
                f"{table} 应为 MUST_RESTORE,实际: {BACKUP_POLICY.get(table)}"

    def test_backup_policy_rebuildable_tables(self):
        """REBUILDABLE 表应包含可重建数据(tasks/notifications)。"""
        from services.backup_schema import BACKUP_POLICY, BackupPolicy

        rebuildable_tables = ["tasks", "notifications"]
        for table in rebuildable_tables:
            assert BACKUP_POLICY.get(table) is BackupPolicy.REBUILDABLE, \
                f"{table} 应为 REBUILDABLE,实际: {BACKUP_POLICY.get(table)}"

    def test_backup_policy_no_export_plaintext_tables(self):
        """NO_EXPORT_PLAINTEXT 表应包含敏感数据(mfa_secrets/sessions)。"""
        from services.backup_schema import BACKUP_POLICY, BackupPolicy

        no_export_tables = ["mfa_secrets", "sessions"]
        for table in no_export_tables:
            assert BACKUP_POLICY.get(table) is BackupPolicy.NO_EXPORT_PLAINTEXT, \
                f"{table} 应为 NO_EXPORT_PLAINTEXT,实际: {BACKUP_POLICY.get(table)}"

    def test_backup_policy_local_only_tables(self):
        """LOCAL_ONLY 表应包含瞬时状态(heartbeat_local/bot_heartbeat)。"""
        from services.backup_schema import BACKUP_POLICY, BackupPolicy

        local_only_tables = ["heartbeat_local", "bot_heartbeat", "decode_logs", "jobs"]
        for table in local_only_tables:
            assert BACKUP_POLICY.get(table) is BackupPolicy.LOCAL_ONLY, \
                f"{table} 应为 LOCAL_ONLY,实际: {BACKUP_POLICY.get(table)}"

    def test_get_backup_policy_unknown_table_returns_local_only(self):
        """未知表名应返回 LOCAL_ONLY(fail-closed)。"""
        from services.backup_schema import get_backup_policy, BackupPolicy

        policy = get_backup_policy("nonexistent_table_xyz")
        assert policy is BackupPolicy.LOCAL_ONLY

    def test_get_backup_policy_known_table_returns_policy(self):
        """已知表名应返回对应的 policy。"""
        from services.backup_schema import get_backup_policy, BackupPolicy

        assert get_backup_policy("users") is BackupPolicy.MUST_RESTORE
        assert get_backup_policy("tasks") is BackupPolicy.REBUILDABLE
        assert get_backup_policy("mfa_secrets") is BackupPolicy.NO_EXPORT_PLAINTEXT
        assert get_backup_policy("heartbeat_local") is BackupPolicy.LOCAL_ONLY

    def test_is_must_restore_helper(self):
        """is_must_restore() 辅助函数应正确识别 MUST_RESTORE 表。"""
        from services.backup_schema import is_must_restore

        assert is_must_restore("users") is True
        assert is_must_restore("tasks") is False
        assert is_must_restore("nonexistent") is False

    def test_is_no_export_helper(self):
        """is_no_export() 辅助函数应正确识别 NO_EXPORT_PLAINTEXT 表。"""
        from services.backup_schema import is_no_export

        assert is_no_export("mfa_secrets") is True
        assert is_no_export("users") is False

    def test_is_local_only_backup_helper(self):
        """is_local_only_backup() 辅助函数应正确识别 LOCAL_ONLY 表。"""
        from services.backup_schema import is_local_only_backup

        assert is_local_only_backup("heartbeat_local") is True
        assert is_local_only_backup("users") is False


class TestP17ValidateBackupReplicationConsistency:
    """R42 P1-7: backup_policy 与 replication_policy 一致性校验。"""

    def test_validate_no_conflict_passes(self):
        """无冲突时 _validate_backup_replication_consistency 应通过。"""
        from services.replication_policy import _validate_backup_replication_consistency

        result = _validate_backup_replication_consistency()
        assert result["is_valid"] is True
        assert result["no_export_crdb_conflicts"] == []

    def test_validate_detects_no_export_crdb_conflict(self, monkeypatch):
        """NO_EXPORT_PLAINTEXT + CRDB 冲突应抛 ValueError。"""
        from services.replication_policy import (
            _validate_backup_replication_consistency,
            TABLE_REPLICATION_POLICY,
            ReplicationPolicy,
        )
        from services.backup_schema import BackupPolicy

        # 模拟冲突:把 mfa_secrets 改为 CRDB 同步策略(原为 LOCAL_ONLY)
        original = TABLE_REPLICATION_POLICY.get("mfa_secrets")
        try:
            TABLE_REPLICATION_POLICY["mfa_secrets"] = ReplicationPolicy.CRDB
            with pytest.raises(ValueError, match="NO_EXPORT_PLAINTEXT"):
                _validate_backup_replication_consistency()
        finally:
            # 恢复原始值
            if original is not None:
                TABLE_REPLICATION_POLICY["mfa_secrets"] = original
            else:
                TABLE_REPLICATION_POLICY.pop("mfa_secrets", None)


class TestP17ApplyBackupPolicy:
    """R42 P1-7: backup_engine._apply_backup_policy 应用策略。"""

    def test_apply_backup_policy_must_restore_keeps_data(self, monkeypatch):
        """MUST_RESTORE 表应完整保留数据。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        backup_data = {
            "tables": {
                "users": [{"user_id": 1}, {"user_id": 2}],
            }
        }
        result = engine._apply_backup_policy(backup_data)
        # users 应完整保留
        assert len(result["tables"]["users"]) == 2

    def test_apply_backup_policy_rebuildable_clears_data(self, monkeypatch):
        """REBUILDABLE 表应清空 rows(仅备份 schema)。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        backup_data = {
            "tables": {
                "tasks": [{"id": 1}, {"id": 2}, {"id": 3}],
            }
        }
        result = engine._apply_backup_policy(backup_data)
        # tasks 应被清空
        assert result["tables"]["tasks"] == []

    def test_apply_backup_policy_no_export_redacts_data(self, monkeypatch):
        """NO_EXPORT_PLAINTEXT 表数据应被替换为 <<REDACTED>>。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        backup_data = {
            "tables": {
                "mfa_secrets": [
                    {"user_id": 1, "secret": "TOTP_SECRET_VALUE"},
                    {"user_id": 2, "secret": "ANOTHER_SECRET"},
                ],
            }
        }
        result = engine._apply_backup_policy(backup_data)
        # mfa_secrets 数据应被替换为 <<REDACTED>>
        rows = result["tables"]["mfa_secrets"]
        assert len(rows) == 2
        for row in rows:
            assert row.get("_redacted") == "<<REDACTED>>"

    def test_apply_backup_policy_local_only_removes_table(self, monkeypatch):
        """LOCAL_ONLY 表应从 backup_data 中移除。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        backup_data = {
            "tables": {
                "users": [{"user_id": 1}],
                "heartbeat_local": [{"slot_id": 1, "fail_streak": 0}],
            }
        }
        result = engine._apply_backup_policy(backup_data)
        # heartbeat_local 应被移除
        assert "heartbeat_local" not in result["tables"]
        # users 应保留
        assert "users" in result["tables"]


class TestP17ApplyRestorePolicy:
    """R42 P1-7: backup_engine._apply_restore_policy 应用恢复策略。"""

    def test_apply_restore_policy_local_only_removes_table(self):
        """LOCAL_ONLY 表不应被恢复(从 data 中移除)。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        data = {
            "tables": {
                "users": [{"user_id": 1}],
                "heartbeat_local": [{"slot_id": 1}],
            }
        }
        result = engine._apply_restore_policy(data)
        assert "heartbeat_local" not in result["tables"]
        assert "users" in result["tables"]

    def test_apply_restore_policy_rebuildable_clears_rows(self):
        """REBUILDABLE 表恢复时应清空 rows(由系统运行时重建)。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        data = {
            "tables": {
                "tasks": [{"id": 1}, {"id": 2}],
            }
        }
        result = engine._apply_restore_policy(data)
        assert result["tables"]["tasks"] == []

    def test_apply_restore_policy_no_export_clears_rows(self):
        """NO_EXPORT_PLAINTEXT 表恢复时应清空 rows(强制用户重新设置)。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        data = {
            "tables": {
                "mfa_secrets": [{"user_id": 1, "secret": "OLD_VALUE"}],
            }
        }
        result = engine._apply_restore_policy(data)
        assert result["tables"]["mfa_secrets"] == []

    def test_apply_restore_policy_must_restore_keeps_data(self):
        """MUST_RESTORE 表应完整恢复(无操作)。"""
        from services.backup_engine import BackupEngine

        engine = BackupEngine(storage=_FakeR2Storage(), cache_store=_FakeCacheStore())
        data = {
            "tables": {
                "users": [{"user_id": 1, "name": "alice"}],
            }
        }
        result = engine._apply_restore_policy(data)
        assert len(result["tables"]["users"]) == 1
        assert result["tables"]["users"][0]["name"] == "alice"


# ════════════════════════════════════════════════════════════════
# P1-9: RPO/RTO 真实 COMPLETE 状态
# ════════════════════════════════════════════════════════════════


class TestP19GetLastSuccessfulBackup:
    """R42 P1-9: BackupEngine.get_last_successful_backup 查询。"""

    @pytest.mark.asyncio
    async def test_get_last_successful_backup_no_history_returns_none(self, monkeypatch):
        """无 backup_history 时应返回 None。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)
        result = await engine.get_last_successful_backup()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_last_successful_backup_with_history_returns_dict(self, monkeypatch):
        """有 status='completed' 记录且 COMPLETE marker 存在时应返回 dict。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 创建一个完整备份(会写入 backup_history + COMPLETE marker)
        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        result = await engine.get_last_successful_backup()
        assert result is not None
        assert isinstance(result, dict)
        assert result["backup_id"] == backup_id
        assert result["status"] == "completed"
        assert result["complete_marker_exists"] is True

    @pytest.mark.asyncio
    async def test_get_last_successful_backup_marker_missing_returns_none(self, monkeypatch):
        """backup_history 显示 completed 但 COMPLETE marker 在 R2 缺失 → 返回 None。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 创建一个完整备份
        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 删除 COMPLETE marker(模拟 R2 中 marker 丢失)
        storage._remove(f"backups/{backup_id}.complete")

        result = await engine.get_last_successful_backup()
        # 由于 marker 缺失,应返回 None(防 backup_history 与 R2 不一致)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_last_successful_backup_skips_non_completed(self, monkeypatch):
        """status != 'completed' 的记录应被跳过。"""
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 手动写入一条 status='failed' 的记录
        history = [
            {
                "backup_id": "backup_failed_0001",
                "status": "failed",
                "complete_marker_exists": False,
                "completed_at": "2026-07-13T10:00:00",
            }
        ]
        await cache.set_kv("backup_history", json.dumps(history))

        result = await engine.get_last_successful_backup()
        assert result is None  # status='failed' 不应被返回


class TestP19RpoCompliance:
    """R42 P1-9: 备份合规状态(RPO)监控指标。"""

    def test_get_backup_compliance_status_no_backup_returns_zero(self, monkeypatch):
        """无 last_backup_at 时 _get_backup_compliance_status 应返回 0。"""
        # 由于 prometheus_exporter 直接读 kv_store,我们通过 mock 验证
        from services import prometheus_exporter as pe

        # mock _read_kv_value 返回空字符串
        monkeypatch.setattr(pe, "_read_kv_value", lambda key, default="": "")
        compliant, rpo = pe._get_backup_compliance_status()
        assert compliant == 0
        assert rpo == -1.0

    def test_get_backup_compliance_status_with_backup_returns_one(self, monkeypatch):
        """有 last_backup_at 时 _get_backup_compliance_status 应返回 1。"""
        from services import prometheus_exporter as pe

        # mock 返回当前时间戳
        recent_ts = str(time.time())
        monkeypatch.setattr(
            pe, "_read_kv_value",
            lambda key, default="": recent_ts if key == "last_backup_at" else "",
        )
        compliant, rpo = pe._get_backup_compliance_status()
        assert compliant == 1
        assert rpo >= 0  # 应为非负数(刚备份)

    def test_get_backup_compliance_status_iso_timestamp(self, monkeypatch):
        """last_backup_at 为 ISO 时间戳时应正确解析。"""
        from services import prometheus_exporter as pe

        # mock 返回 ISO 时间戳(当前 UTC)
        iso_ts = datetime.now(timezone.utc).isoformat()
        monkeypatch.setattr(
            pe, "_read_kv_value",
            lambda key, default="": iso_ts if key == "last_backup_at" else "",
        )
        compliant, rpo = pe._get_backup_compliance_status()
        assert compliant == 1
        assert rpo >= 0

    def test_get_backup_compliance_status_invalid_returns_zero(self, monkeypatch):
        """last_backup_at 无法解析时应返回 0。"""
        from services import prometheus_exporter as pe

        monkeypatch.setattr(
            pe, "_read_kv_value",
            lambda key, default="": "not_a_timestamp" if key == "last_backup_at" else "",
        )
        compliant, rpo = pe._get_backup_compliance_status()
        assert compliant == 0
        assert rpo == -1.0


class TestP19PrometheusMetrics:
    """R42 P1-9: Prometheus exporter 暴露的 backup_compliant 指标。"""

    def test_collect_metrics_includes_backup_compliant_gauge(self, monkeypatch):
        """collect_metrics 输出应包含 tgjiema_backup_compliant gauge。"""
        from services import prometheus_exporter as pe

        # mock 所有 SQLite 读取避免依赖真实数据库
        monkeypatch.setattr(pe, "_read_kv_value", lambda key, default="0": "0")
        monkeypatch.setattr(pe, "_read_sqlite_single", lambda db, query, default=0: 0)
        monkeypatch.setattr(pe, "_get_relay_spool_disk_usage", lambda: 0)

        # 不启动 R40 采集线程(避免后台线程副作用)
        monkeypatch.setattr(pe, "_start_r40_collector", lambda: None)
        # 不调用 check_readiness(避免依赖 SQLite)
        monkeypatch.setattr(
            pe, "check_readiness",
            lambda: {"ready": False, "passed": 0, "checks": {}, "details": {},
                     "ru_daily_usage": "unknown", "last_crdb_sync_age": -1,
                     "last_r2_collect_age": -1},
        )

        output = pe.collect_metrics()

        # 应包含 tgjiema_backup_compliant gauge
        assert "tgjiema_backup_compliant" in output
        assert "# HELP tgjiema_backup_compliant" in output
        assert "# TYPE tgjiema_backup_compliant gauge" in output

    def test_collect_metrics_includes_backup_rpo_seconds_gauge(self, monkeypatch):
        """collect_metrics 输出应包含 tgjiema_backup_rpo_seconds gauge。"""
        from services import prometheus_exporter as pe

        monkeypatch.setattr(pe, "_read_kv_value", lambda key, default="0": "0")
        monkeypatch.setattr(pe, "_read_sqlite_single", lambda db, query, default=0: 0)
        monkeypatch.setattr(pe, "_get_relay_spool_disk_usage", lambda: 0)
        monkeypatch.setattr(pe, "_start_r40_collector", lambda: None)
        monkeypatch.setattr(
            pe, "check_readiness",
            lambda: {"ready": False, "passed": 0, "checks": {}, "details": {},
                     "ru_daily_usage": "unknown", "last_crdb_sync_age": -1,
                     "last_r2_collect_age": -1},
        )

        output = pe.collect_metrics()
        assert "tgjiema_backup_rpo_seconds" in output
        assert "# HELP tgjiema_backup_rpo_seconds" in output

    def test_collect_metrics_backup_compliant_zero_without_backup(self, monkeypatch):
        """无 last_backup_at 时 tgjiema_backup_compliant=0。"""
        from services import prometheus_exporter as pe

        # mock 返回空(无 last_backup_at)
        def _mock_kv(key, default="0"):
            if key == "last_backup_at":
                return ""
            return "0"
        monkeypatch.setattr(pe, "_read_kv_value", _mock_kv)
        monkeypatch.setattr(pe, "_read_sqlite_single", lambda db, query, default=0: 0)
        monkeypatch.setattr(pe, "_get_relay_spool_disk_usage", lambda: 0)
        monkeypatch.setattr(pe, "_start_r40_collector", lambda: None)
        monkeypatch.setattr(
            pe, "check_readiness",
            lambda: {"ready": False, "passed": 0, "checks": {}, "details": {},
                     "ru_daily_usage": "unknown", "last_crdb_sync_age": -1,
                     "last_r2_collect_age": -1},
        )

        output = pe.collect_metrics()
        # 应包含 tgjiema_backup_compliant 0
        # 找到该行
        for line in output.split("\n"):
            if line.startswith("tgjiema_backup_compliant "):
                assert line.endswith(" 0"), \
                    f"无备份时 backup_compliant 应为 0,实际: {line}"
                return
        pytest.fail("未找到 tgjiema_backup_compliant 指标行")

    def test_collect_metrics_backup_compliant_one_with_backup(self, monkeypatch):
        """有 last_backup_at 时 tgjiema_backup_compliant=1。"""
        from services import prometheus_exporter as pe

        recent_ts = str(time.time())
        def _mock_kv(key, default="0"):
            if key == "last_backup_at":
                return recent_ts
            return "0"
        monkeypatch.setattr(pe, "_read_kv_value", _mock_kv)
        monkeypatch.setattr(pe, "_read_sqlite_single", lambda db, query, default=0: 0)
        monkeypatch.setattr(pe, "_get_relay_spool_disk_usage", lambda: 0)
        monkeypatch.setattr(pe, "_start_r40_collector", lambda: None)
        monkeypatch.setattr(
            pe, "check_readiness",
            lambda: {"ready": False, "passed": 0, "checks": {}, "details": {},
                     "ru_daily_usage": "unknown", "last_crdb_sync_age": -1,
                     "last_r2_collect_age": -1},
        )

        output = pe.collect_metrics()
        for line in output.split("\n"):
            if line.startswith("tgjiema_backup_compliant "):
                assert line.endswith(" 1"), \
                    f"有备份时 backup_compliant 应为 1,实际: {line}"
                return
        pytest.fail("未找到 tgjiema_backup_compliant 指标行")


# ════════════════════════════════════════════════════════════════
# 集成测试: create_backup → get_last_successful_backup
# ════════════════════════════════════════════════════════════════


class TestP19BackupHistoryIntegration:
    """R42 P1-9: create_backup 与 get_last_successful_backup 集成测试。"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_create_backup_writes_history(self, monkeypatch):
        """create_backup 成功后应写入 backup_history(status='completed')。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        backup_id = manifest["backup_id"]

        # 验证 backup_history 已写入
        raw = await cache.get_kv("backup_history")
        assert raw is not None
        history = json.loads(raw)
        assert isinstance(history, list)
        assert len(history) >= 1
        latest = history[-1]
        assert latest["backup_id"] == backup_id
        assert latest["status"] == "completed"
        assert latest["complete_marker_exists"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_create_backup_updates_last_backup_at(self, monkeypatch):
        """create_backup 成功后应更新 last_backup_at(用于 RPO 计算)。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        manifest = await engine.create_backup(backup_type="full")
        # last_backup_at 应等于 manifest.created_at
        last_at = await cache.get_kv("last_backup_at")
        assert last_at == manifest["created_at"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _ENCRYPT_AVAILABLE, reason="cryptography 不可用")
    async def test_get_last_successful_backup_returns_latest(self, monkeypatch):
        """多个成功备份时应返回最新的一个。"""
        _patch_backup_all_tables(monkeypatch)
        engine, storage, cache, _ = _build_engine_with_kek(monkeypatch)

        # 创建两个备份
        manifest1 = await engine.create_backup(backup_type="full")
        # 等待至少 1 秒确保时间戳不同
        import asyncio as _asyncio
        await _asyncio.sleep(1.1)
        manifest2 = await engine.create_backup(backup_type="full")

        result = await engine.get_last_successful_backup()
        # 应返回最新创建的备份
        assert result["backup_id"] == manifest2["backup_id"]
