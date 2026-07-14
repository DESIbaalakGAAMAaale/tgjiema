"""R49 P0-4: Critical Effect request_hash 强制约束深度整改测试。

测试覆盖:
1. record_pending: critical effect + 空 request_hash → raise ValueError(应用层校验)
2. record_pending: critical effect + 非空 request_hash → 成功
3. record_pending: 非 critical effect + 空 request_hash → 成功
4. record_completed: request_hash 不匹配 → raise EffectReceiptError(防 completed 阶段替换 payload)
5. record_completed: request_hash 匹配 → 成功
6. 覆盖率扫描器: 检测高风险 action 使用旧 sync API generate_signed_callback

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据),由 CacheStore.init() 创建 effect_receipts 表
  (R49 P0-4 DDL: request_hash TEXT NOT NULL + CHECK 约束)
- 通过 monkeypatch 注入测试 store 到 EffectReceiptManager 单例
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(由 init() 创建含 R49 P0-4 约束的表)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建 effect_receipts 表(R49 P0-4 DDL:
    request_hash TEXT NOT NULL + CHECK 约束)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r49_p0_4_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_receipt_manager_singleton():
    """每个用例前重置 EffectReceiptManager 单例,避免跨用例污染。"""
    from services import effect_receipts as er_mod
    er_mod._receipt_manager = None
    yield
    er_mod._receipt_manager = None


# ════════════════════════════════════════════════════════════════
# 1. record_pending: critical effect request_hash 强制校验
# ════════════════════════════════════════════════════════════════

class TestRecordPendingRequestHashEnforced:
    """R49 P0-4: record_pending 对 critical effect 的 request_hash 强制校验。"""

    @pytest.mark.asyncio
    async def test_record_pending_critical_with_empty_hash_raises(self, real_store):
        """critical effect + request_hash="" 必须 raise ValueError。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        with pytest.raises(ValueError, match="request_hash"):
            await mgr.record_pending(
                "act_crit_empty_hash", "telegram_send", "chat_1",
                request_hash="",
            )

    @pytest.mark.asyncio
    async def test_record_pending_critical_with_nonempty_hash_succeeds(self, real_store):
        """critical effect + request_hash="abc" 成功 claim。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        ok = await mgr.record_pending(
            "act_crit_ok_hash", "telegram_send", "chat_1",
            request_hash="abc123",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_record_pending_non_critical_with_empty_hash_succeeds(self, real_store):
        """非 critical effect + request_hash="" 成功(允许空 hash)。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        ok = await mgr.record_pending(
            "act_noncrit_empty_hash", "sqlite_write", "audit_log:1",
            request_hash="",
        )
        assert ok is True


# ════════════════════════════════════════════════════════════════
# 2. record_completed: request_hash 一致性校验
# ════════════════════════════════════════════════════════════════

class TestRecordCompletedRequestHashConsistency:
    """R49 P0-4: record_completed 的 expected_request_hash 一致性校验。"""

    @pytest.mark.asyncio
    async def test_record_completed_with_mismatched_hash_raises(self, real_store):
        """record_pending(hash=A) → record_completed(expected_hash=B) 必须 raise EffectReceiptError。"""
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)
        await mgr.record_pending(
            "act_mismatch", "telegram_send", "chat_1",
            request_hash="hash_A",
        )
        with pytest.raises(EffectReceiptError, match="request_hash 不匹配"):
            await mgr.record_completed(
                "act_mismatch", "telegram_send", "chat_1",
                external_id="msg_1", expected_request_hash="hash_B",
            )

    @pytest.mark.asyncio
    async def test_record_completed_with_matching_hash_succeeds(self, real_store):
        """record_pending(hash=A) → record_completed(expected_hash=A) 成功。"""
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        await mgr.record_pending(
            "act_match", "telegram_send", "chat_1",
            request_hash="hash_A",
        )
        await mgr.record_completed(
            "act_match", "telegram_send", "chat_1",
            external_id="msg_1", expected_request_hash="hash_A",
        )
        result = await mgr.check_receipt("act_match", "telegram_send", "chat_1")
        assert result is not None
        assert result["status"] == "completed"
        assert result["external_id"] == "msg_1"


# ════════════════════════════════════════════════════════════════
# 3. 覆盖率扫描器: 高风险 sync API 检测
# ════════════════════════════════════════════════════════════════

class TestCoverageScannerSyncApi:
    """R49 P0-4: 覆盖率扫描器检测高风险 action 使用旧 sync API。"""

    def test_coverage_scanner_detects_sync_api_for_high_risk_action(self, tmp_path):
        """构造临时 .py 文件含 generate_signed_callback(callback_data="delete_user_123"),
        scanner 应报告违规。
        """
        from services.effect_receipts import validate_critical_effects_have_action_id

        # 构造临时项目结构: tmp_path/services/sync_api_demo.py
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        py_file = services_dir / "sync_api_demo.py"
        py_file.write_text(
            'generate_signed_callback(callback_data="delete_user_123")\n',
            encoding="utf-8",
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        sync_violations = [
            v for v in violations
            if v.get("call") == "generate_signed_callback"
        ]
        assert len(sync_violations) >= 1, (
            f"应报告 generate_signed_callback 违规,实际: {violations}"
        )
        assert "delete" in sync_violations[0]["reason"]
        assert sync_violations[0]["effect_type"] == "delete"

    def test_coverage_scanner_no_violation_for_low_risk_action(self, tmp_path):
        """低风险 action(如 confirm/cancel)使用 generate_signed_callback 不应报告违规。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        py_file = services_dir / "sync_api_safe.py"
        py_file.write_text(
            'generate_signed_callback(callback_data="confirm_user_123")\n',
            encoding="utf-8",
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        sync_violations = [
            v for v in violations
            if v.get("call") == "generate_signed_callback"
        ]
        assert len(sync_violations) == 0, (
            f"低风险 action 不应报告违规,实际: {sync_violations}"
        )
