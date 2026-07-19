"""R64 P0-04: outbox 生产闭环证据 — 终审报告 P0-04 整改测试。

被测目标(R64 P0-04 整改要求):
- ``services.data_lifecycle.OutboxEnvelope`` —— immutable dataclass(frozen=True):
    * provider 接收 immutable 信封(基于 idempotency_key 去重)
    * frozen=True 防 provider 篡改 effect_type / request_hash 绕过 CAS
- ``services.data_lifecycle.OutboxWorker.validate_providers`` —— 不再 fail-open:
    * CRITICAL_EFFECT_TYPES 导入失败 → raise AppError(OUTBOX_PROVIDER_REGISTRY_LOAD_FAILED)
    * 严禁 ``except Exception: return []`` 兜底绕过校验
- ``services.data_lifecycle.OutboxWorker.run_once`` —— 生产构建移除 stub 分支:
    * ``provider_registry is None`` 一律 raise
      ``AppError(OUTBOX_PROVIDER_REGISTRY_REQUIRED)``
    * R65 P1-05: ``test_mode`` 参数已彻底移除;测试注入独立 fake provider
- ``database.cache_store.CacheStore`` outbox_events CAS 升级(lease_version fencing):
    * R65 P1-05: ``claim_outbox_events`` CAS WHERE status='pending',
      成功后 ``lease_version = lease_version + 1``(单调递增,永不重置)
    * ``complete_outbox_event`` / ``fail_outbox_event`` / ``renew_outbox_lease``
      四字段 CAS(event_id + owner + lease_version + request_hash)
    * R65 P1-05: 严格 CAS 路径冲突 raise
      ``AppError(OUTBOX_LEASE_VERSION_CONFLICT)``
    * R65 P1-05: ``reclaim_stale_outbox_leases`` / ``fail_outbox_event``
      retryable 路径不再重置 lease_version=0(保留单调递增)
    * ``renew_outbox_lease`` 成功后 lease_version += 1(防 ABA)
- ``services.data_lifecycle.OutboxWorker._maybe_renew_lease`` —— 自动续租:
    * lease 剩余 < 1/3 即自动续租
    * 续租失败立即停止提交结果(raise AppError(OUTBOX_LEASE_RENEW_FAILED))
- ``database.cache_store.CacheStore.move_outbox_to_dlq`` —— DLQ 闭环:
    * 写入 dlq_reason / dlq_at 审计字段
    * 写入 outbox_dlq_audit 审计记录(可审批 replay)
    * worker 调用后 logger.error 告警
- ``services.outbox_worker.OutboxWorker._dispatch_event`` —— 旧 worker fail-closed:
    * 未知 event_type 改为 raise AppError(OUTBOX_EVENT_UNKNOWN)(进入 DLQ)
    * 不再静默视为完成

测试覆盖(6 项):
1. OutboxEnvelope immutable(frozen=True,字段不可变 + 完整字段)
2. validate_providers() 不再 fail-open(CRITICAL_EFFECT_TYPES 导入失败 raise)
3. run_once 无 provider 仍 raise(生产构建移除 stub 分支;R65: test_mode 已移除)
4. lease_version fencing token CAS(claim/complete/fail/renew 四字段 CAS;
   R65: 严格 CAS 冲突 raise OUTBOX_LEASE_VERSION_CONFLICT)
5. 自动续租(lease 剩余 < 1/3 续租;续租失败 raise AppError 停止提交)
6. DLQ 闭环(move_outbox_to_dlq 写入 dlq_reason/dlq_at + outbox_dlq_audit 审计记录)
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from services.error_codes import AppError, ErrorCodes

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
# Fixture: 真实 SQLite 临时数据库(由 init() 创建含 R64 P0-04 约束的表)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建 outbox_events 表(含 R64 P0-04 新增
    lease_version / dlq_reason / dlq_at 列 + outbox_dlq_audit 审计表)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r64_p0_4_test_")
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


@pytest.fixture(autouse=True)
def _reset_receipt_manager_singleton():
    """每个用例前重置 EffectReceiptManager 单例,避免跨用例污染。"""
    from services import effect_receipts as er_mod
    er_mod._receipt_manager = None
    yield
    er_mod._receipt_manager = None


# ════════════════════════════════════════════════════════════════
# 1. OutboxEnvelope immutable(frozen=True)
# ════════════════════════════════════════════════════════════════

class TestOutboxEnvelopeImmutable:
    """R64 P0-04: OutboxEnvelope 是 immutable dataclass(frozen=True)。"""

    def test_envelope_is_frozen_dataclass(self):
        """OutboxEnvelope 必须是 @dataclass(frozen=True)。"""
        from services.data_lifecycle import OutboxEnvelope
        from dataclasses import is_dataclass
        assert is_dataclass(OutboxEnvelope)
        # frozen=True 时 setting 字段会 raise FrozenInstanceError
        env = OutboxEnvelope(
            event_id=1,
            effect_type="telegram_send",
            target="chat:1",
            request_hash="rh" + "0" * 62,
            idempotency_key="act:rh",
            payload_digest="d" * 64,
            payload={"k": "v"},
        )
        with pytest.raises(Exception):
            env.effect_type = "tampered"
        with pytest.raises(Exception):
            env.request_hash = "forged"
        with pytest.raises(Exception):
            env.event_id = 999

    def test_envelope_field_set_complete(self):
        """OutboxEnvelope 字段集完整(event_id/effect_type/target/request_hash/
        idempotency_key/payload_digest/payload)。"""
        from services.data_lifecycle import OutboxEnvelope
        env = OutboxEnvelope(
            event_id=42,
            effect_type="r2_put",
            target="bucket/key",
            request_hash="rh_1" + "0" * 60,
            idempotency_key="act_42:rh_1...",
            payload_digest="ab" * 32,
            payload={"size": 1024, "etag": "abc"},
        )
        assert env.event_id == 42
        assert env.effect_type == "r2_put"
        assert env.target == "bucket/key"
        assert env.request_hash == "rh_1" + "0" * 60
        assert env.idempotency_key == "act_42:rh_1..."
        assert env.payload_digest == "ab" * 32
        assert env.payload["size"] == 1024
        assert env.payload["etag"] == "abc"

    def test_envelope_payload_is_mapping(self):
        """payload 字段类型为 Mapping(只读 view)。"""
        from services.data_lifecycle import OutboxEnvelope
        from typing import Mapping
        env = OutboxEnvelope(
            event_id=1, effect_type="purge", target="db:users",
            request_hash="rh", idempotency_key="a:rh",
            payload_digest="d" * 64, payload={"k": "v"},
        )
        # isinstance(Mapping) 检查(dict 是 Mapping 的实现)
        assert isinstance(env.payload, Mapping)


# ════════════════════════════════════════════════════════════════
# 2. validate_providers() 不再 fail-open
# ════════════════════════════════════════════════════════════════

class TestValidateProvidersNoFailOpen:
    """R64 P0-04: validate_providers() 严禁 fail-open。

    旧实现 ``except Exception: return []`` 兜底,CRITICAL_EFFECT_TYPES 导入失败时
    返回空列表(不阻断),生产环境若 effect_receipts 模块损坏,worker 会误判
    readiness OK 并启动,所有 effect_type 视为已覆盖。
    新实现:任何 registry/schema 加载异常直接 raise
    ``AppError(OUTBOX_PROVIDER_REGISTRY_LOAD_FAILED)``。
    """

    def test_validate_providers_raises_when_critical_effect_types_import_fails(
        self, monkeypatch,
    ):
        """CRITICAL_EFFECT_TYPES 导入失败 → raise AppError(LOAD_FAILED)。"""
        from services.data_lifecycle import OutboxWorker
        worker = OutboxWorker(lease_owner="w", batch_size=1)
        # 模拟 effect_receipts 模块导入失败(注入 ImportError 到 sys.modules)
        # 通过 monkeypatch 替换 services.effect_receipts 模块属性,触发 ImportError
        # 由于 validate_providers 内部用 ``from services.effect_receipts import
        # CRITICAL_EFFECT_TYPES``,我们直接 monkeypatch sys.modules 让该 import 抛错
        import services.effect_receipts as er_mod
        # 删除 CRITICAL_EFFECT_TYPES 属性 + 让 __getattr__ 抛 AttributeError
        monkeypatch.delattr(er_mod, "CRITICAL_EFFECT_TYPES", raising=False)
        # 在 sys.modules 中替换为一个会抛 ImportError 的 mock
        original_module = sys.modules.get("services.effect_receipts")
        failing_mod = MagicMock()
        del failing_mod.CRITICAL_EFFECT_TYPES  # 触发 AttributeError
        # 让 hasattr / getattr 抛 AttributeError
        type(failing_mod).CRITICAL_EFFECT_TYPES = property(
            lambda self: (_ for _ in ()).throw(ImportError("simulated import failure"))
        )
        sys.modules["services.effect_receipts"] = failing_mod
        try:
            with pytest.raises(AppError) as exc_info:
                worker.validate_providers()
            # 验证错误码
            assert exc_info.value.code == ErrorCodes.OUTBOX_PROVIDER_REGISTRY_LOAD_FAILED
        finally:
            # 恢复 sys.modules
            if original_module is not None:
                sys.modules["services.effect_receipts"] = original_module

    def test_validate_providers_no_failopen_returns_missing_list_for_none_registry(self):
        """provider_registry=None → 返回全部 missing(不 raise,正常报告缺失)。"""
        from services.data_lifecycle import OutboxWorker
        from services.effect_receipts import CRITICAL_EFFECT_TYPES
        # R65 P1-05: test_mode 参数已移除,不再传入
        worker = OutboxWorker(lease_owner="w", batch_size=1)
        missing = worker.validate_providers()
        # 9 个枚举 effect types 全部 missing
        assert len(missing) == len(CRITICAL_EFFECT_TYPES)
        for et in CRITICAL_EFFECT_TYPES:
            assert et in missing

    def test_validate_providers_full_coverage_returns_empty(self):
        """所有 CRITICAL_EFFECT_TYPES 都有 provider → 返回空 list(readiness OK)。"""
        from services.data_lifecycle import OutboxWorker
        from services.effect_receipts import CRITICAL_EFFECT_TYPES
        registry = {et: AsyncMock() for et in CRITICAL_EFFECT_TYPES}
        worker = OutboxWorker(
            lease_owner="w", batch_size=1,
            provider_registry=registry,
        )
        missing = worker.validate_providers()
        assert missing == []


# ════════════════════════════════════════════════════════════════
# 3. run_once 无 provider 仍 raise(生产构建移除 stub 分支;R65: test_mode 已移除)
# ════════════════════════════════════════════════════════════════

class TestRunOnceNoStubBranch:
    """R64 P0-04 + R65 P1-05: provider_registry=None 一律 raise AppError。

    旧 R63 实现在 test_mode=True 时允许 stub 模式直接 complete;
    R64 P0-04 整改:生产构建从代码层移除 no-provider-complete 分支,
    测试注入独立 fake provider。
    R65 P1-05 整改:``test_mode`` 参数彻底移除,传入即 TypeError。
    """

    @pytest.mark.asyncio
    async def test_run_once_raises_with_no_provider(
        self, real_store,
    ):
        """provider_registry=None → raise(不再 stub complete)。"""
        from services.data_lifecycle import OutboxWorker
        await real_store.add_outbox_event(
            action_id="act_p0_4_no_stub",
            effect_type="telegram_send",
            target="chat:1",
            request_hash="rh_p0_4_no_stub" + "0" * 47,
            payload_json="{}",
        )
        # R65 P1-05: test_mode 参数已移除,不传入
        worker = OutboxWorker(
            lease_owner="worker_no_stub", batch_size=10,
        )
        with pytest.raises(AppError):
            await worker.run_once()
        # 验证 fail-fast 发生在 claim 之前(事件仍为 pending)
        cursor = await real_store._db.execute(
            "SELECT status FROM outbox_events WHERE action_id=?",
            ("act_p0_4_no_stub",),
        )
        row = await cursor.fetchone()
        assert row[0] == "pending"

    def test_test_mode_parameter_removed_raises_typeerror(self):
        """R65 P1-05: ``test_mode`` 参数已彻底移除,传入即 TypeError。"""
        from services.data_lifecycle import OutboxWorker
        with pytest.raises(TypeError):
            OutboxWorker(
                lease_owner="w", batch_size=1, test_mode=True,
            )

    @pytest.mark.asyncio
    async def test_run_once_with_fake_provider_completes(self, real_store):
        """注入 fake provider → run_once 正常 complete(测试应注入 fake provider)。"""
        from services.data_lifecycle import OutboxEnvelope, OutboxWorker

        async def _fake_provider(envelope: OutboxEnvelope):
            assert isinstance(envelope, OutboxEnvelope)
            return f"ext_{envelope.event_id}"

        await real_store.add_outbox_event(
            action_id="act_p0_4_fake",
            effect_type="telegram_send",
            target="chat:1",
            request_hash="rh_p0_4_fake" + "0" * 49,
            payload_json=json.dumps({"text": "hi"}),
        )
        worker = OutboxWorker(
            lease_owner="worker_fake", batch_size=10,
            provider_registry={"telegram_send": _fake_provider},
        )
        result = await worker.run_once()
        assert result["claimed"] == 1
        assert result["completed"] == 1
        assert result["failed"] == 0
        assert result["dlq"] == 0


# ════════════════════════════════════════════════════════════════
# 4. lease_version fencing token CAS(claim/complete/fail/renew 四字段 CAS)
# ════════════════════════════════════════════════════════════════

class TestLeaseVersionFencingCAS:
    """R64 P0-04 + R65 P1-05: lease fencing token CAS(防 ABA 问题)。

    R65 P1-05: claim CAS WHERE status='pending',成功后
    ``lease_version = lease_version + 1``(单调递增,永不重置);
    complete/fail/renew 必须 CAS event_id+owner+lease_version+request_hash;
    严格 CAS 路径冲突 raise ``AppError(OUTBOX_LEASE_VERSION_CONFLICT)``,
    不再静默返回 False/not_found;
    reclaim/fail retryable 路径不重置 lease_version(保留单调递增);
    renew 成功后 lease_version += 1(后续 complete 必须用新版本号)。
    """

    @pytest.mark.asyncio
    async def test_claim_sets_lease_version_to_1(self, real_store):
        """claim_outbox_events 成功后 lease_version=1(从 0 升级)。"""
        rh = "rh_lv_claim" + "0" * 52
        eid = await real_store.add_outbox_event(
            action_id="act_lv_claim",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        # 添加后 lease_version=0(DEFAULT)
        cursor = await real_store._db.execute(
            "SELECT lease_version, status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 0
        assert row[1] == "pending"
        # claim
        events = await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        assert len(events) == 1
        # R64 P0-04 / R65 P1-05: claim 后 lease_version=1(0+1 单调递增)
        assert events[0]["lease_version"] == 1
        cursor = await real_store._db.execute(
            "SELECT lease_version, status, lease_owner FROM outbox_events "
            "WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1
        assert row[1] == "in_flight"
        assert row[2] == "worker_A"

    @pytest.mark.asyncio
    async def test_complete_with_wrong_lease_version_raises_conflict(self, real_store):
        """R65 P1-05: complete CAS lease_version 不匹配 → raise AppError(冲突)。"""
        rh = "rh_lv_complete" + "0" * 49
        eid = await real_store.add_outbox_event(
            action_id="act_lv_complete",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # 用错误的 lease_version(0 而非 1)→ CAS 冲突 raise
        with pytest.raises(AppError) as exc_info:
            await real_store.complete_outbox_event(
                eid, external_id="ext",
                lease_owner="worker_A", request_hash=rh,
                lease_version=0,  # 错误(应为 1)
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        # 用正确的 lease_version=1 → CAS 成功
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_A", request_hash=rh,
            lease_version=1,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_renew_increments_lease_version(self, real_store):
        """renew_outbox_lease 成功后 lease_version += 1。"""
        rh = "rh_lv_renew" + "0" * 52
        eid = await real_store.add_outbox_event(
            action_id="act_lv_renew",
            effect_type="r2_put",
            target="bucket/key",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_long", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1(claim 后)
        # renew with lease_version=1 → 成功 + lease_version → 2
        ok = await real_store.renew_outbox_lease(
            eid, lease_owner="worker_long", request_hash=rh,
            lease_version=1, lease_duration_seconds=300,
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT lease_version FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 2  # 递增
        # R65 P1-05: 用旧 lease_version=1 再 renew → raise AppError(版本过期)
        with pytest.raises(AppError) as exc_info:
            await real_store.renew_outbox_lease(
                eid, lease_owner="worker_long", request_hash=rh,
                lease_version=1, lease_duration_seconds=300,
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        # 用新 lease_version=2 renew → 成功 + lease_version → 3
        ok = await real_store.renew_outbox_lease(
            eid, lease_owner="worker_long", request_hash=rh,
            lease_version=2, lease_duration_seconds=300,
        )
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT lease_version FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 3

    @pytest.mark.asyncio
    async def test_complete_after_renew_uses_new_lease_version(self, real_store):
        """renew 后 complete 必须用新 lease_version(旧版本 raise 冲突)。"""
        rh = "rh_lv_after_renew" + "0" * 47
        eid = await real_store.add_outbox_event(
            action_id="act_lv_after_renew",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # lease_version=1;renew → lease_version=2
        await real_store.renew_outbox_lease(
            eid, lease_owner="worker_A", request_hash=rh,
            lease_version=1, lease_duration_seconds=300,
        )
        # R65 P1-05: 用旧 lease_version=1 complete → raise AppError(防 ABA)
        with pytest.raises(AppError) as exc_info:
            await real_store.complete_outbox_event(
                eid, external_id="ext",
                lease_owner="worker_A", request_hash=rh,
                lease_version=1,  # 旧版本
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        # 用新 lease_version=2 complete → 成功
        ok = await real_store.complete_outbox_event(
            eid, external_id="ext",
            lease_owner="worker_A", request_hash=rh,
            lease_version=2,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_fail_with_wrong_lease_version_raises_conflict(self, real_store):
        """R65 P1-05: fail CAS lease_version 不匹配 → raise AppError(防越权 fail)。"""
        rh = "rh_lv_fail" + "0" * 54
        eid = await real_store.add_outbox_event(
            action_id="act_lv_fail",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
            max_attempts=3,
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_A", lease_duration_seconds=60, limit=1,
        )
        # R65 P1-05: 用错误的 lease_version=99 → raise AppError
        with pytest.raises(AppError) as exc_info:
            await real_store.fail_outbox_event(
                eid, error_msg="boom",
                lease_owner="worker_A", request_hash=rh,
                lease_version=99,
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT
        # 用正确的 lease_version=1 → retryable
        result = await real_store.fail_outbox_event(
            eid, error_msg="boom",
            lease_owner="worker_A", request_hash=rh,
            lease_version=1,
        )
        assert result == "retryable"
        # R65 P1-05: lease_version 不再重置为 0(保留单调递增,防 ABA)
        # 当前 lease_version 仍为 1(retryable 路径仅清 owner/status,不重置版本)
        cursor = await real_store._db.execute(
            "SELECT lease_version, status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 1  # 保留 lease_version(reclaim/fail 不重置)
        assert row[1] == "pending"


# ════════════════════════════════════════════════════════════════
# 5. 自动续租(lease 剩余 < 1/3 续租;续租失败 raise AppError)
# ════════════════════════════════════════════════════════════════

class TestAutoLeaseRenewal:
    """R64 P0-04: provider 调用超过租期三分之一即自动续租,续租失败立即停止提交结果。

    _maybe_renew_lease 检查 lease 剩余时间,< lease_duration / 3 即续租;
    续租成功后 lease_version += 1;
    续租失败(lease 已被回收 / 版本不匹配)raise AppError(OUTBOX_LEASE_RENEW_FAILED)。
    """

    @pytest.mark.asyncio
    async def test_maybe_renew_lease_skips_when_remaining_above_threshold(
        self, real_store,
    ):
        """lease 剩余 > 1/3 → 不续租(返回 renewed=False)。"""
        from services.data_lifecycle import OutboxWorker
        rh = "rh_renew_skip" + "0" * 51
        eid = await real_store.add_outbox_event(
            action_id="act_renew_skip",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_renew", lease_duration_seconds=60, limit=1,
        )
        worker = OutboxWorker(
            lease_owner="worker_renew",
            lease_duration_seconds=60,
            batch_size=1,
        )
        # lease 刚 claim,剩余 > 1/3(60/3=20s)→ 不续租
        future_iso = (datetime.utcnow() + timedelta(seconds=50)).isoformat()
        renewed, new_lv, new_exp = await worker._maybe_renew_lease(
            real_store, eid, rh, 1, future_iso,
        )
        assert renewed is False
        assert new_lv == 1
        assert new_exp == future_iso

    @pytest.mark.asyncio
    async def test_maybe_renew_lease_renews_when_remaining_below_threshold(
        self, real_store,
    ):
        """lease 剩余 < 1/3 → 自动续租(lease_version += 1)。"""
        from services.data_lifecycle import OutboxWorker
        rh = "rh_renew_below" + "0" * 50
        eid = await real_store.add_outbox_event(
            action_id="act_renew_below",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_renew", lease_duration_seconds=60, limit=1,
        )
        worker = OutboxWorker(
            lease_owner="worker_renew",
            lease_duration_seconds=60,
            batch_size=1,
        )
        # lease 剩余 10s < 60/3=20s → 续租
        near_expiry = (datetime.utcnow() + timedelta(seconds=10)).isoformat()
        renewed, new_lv, new_exp = await worker._maybe_renew_lease(
            real_store, eid, rh, 1, near_expiry,
        )
        assert renewed is True
        assert new_lv == 2  # 递增
        # 新过期时间应在 now + 60s 附近(允许 ±5s 误差)
        new_exp_dt = datetime.fromisoformat(new_exp)
        now = datetime.utcnow()
        assert now + timedelta(seconds=55) < new_exp_dt < now + timedelta(seconds=65)

    @pytest.mark.asyncio
    async def test_maybe_renew_lease_raises_when_renew_fails(self, real_store):
        """续租失败(版本不匹配 / lease 已被回收)→ raise AppError(RENEW_FAILED)。"""
        from services.data_lifecycle import OutboxWorker
        rh = "rh_renew_fail" + "0" * 51
        eid = await real_store.add_outbox_event(
            action_id="act_renew_fail",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_owner", lease_duration_seconds=60, limit=1,
        )
        # worker 用错误的 lease_owner(不是真正的 lease 持有者)
        worker = OutboxWorker(
            lease_owner="worker_impostor",
            lease_duration_seconds=60,
            batch_size=1,
        )
        # lease 剩余 5s < 20s 阈值 → 触发续租,但 worker 不是 lease 持有者 → 失败
        near_expiry = (datetime.utcnow() + timedelta(seconds=5)).isoformat()
        with pytest.raises(AppError) as exc_info:
            await worker._maybe_renew_lease(
                real_store, eid, rh, 1, near_expiry,
            )
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_RENEW_FAILED

    @pytest.mark.asyncio
    async def test_process_event_raises_when_watchdog_renew_fails(self, real_store):
        """provider 长调用期间续租 watchdog 失败 → raise AppError 停止提交结果。

        场景:provider 调用期间 lease 被其他 worker 回收,watchdog 续租失败,
        process_event 应立即停止提交结果(raise AppError(RENEW_FAILED),
        不会调用 complete_outbox_event)。

        时序设计:
        - lease_duration_seconds=0.6 → 续租阈值 0.2s,watchdog 间隔 0.1s
        - lease_expires_at = now + 0.1s(< 0.2s 阈值,首次 watchdog 检查即续租)
        - provider sleep 1.0s(确保 watchdog 先触发续租)
        - worker_impostor 不是真正 lease 持有者 → renew CAS 失败 → raise AppError
        """
        from services.data_lifecycle import OutboxEnvelope, OutboxWorker

        async def _slow_provider(envelope: OutboxEnvelope):
            # 模拟长 provider 调用(确保 watchdog 有时间触发续租检查)
            await asyncio.sleep(1.0)
            return "ext_slow"

        rh = "rh_watchdog_fail" + "0" * 48
        eid = await real_store.add_outbox_event(
            action_id="act_watchdog_fail",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_owner", lease_duration_seconds=60, limit=1,
        )
        # 用错误的 lease_owner → watchdog 续租时 CAS 失败
        worker = OutboxWorker(
            lease_owner="worker_impostor",  # 不是 lease 持有者
            lease_duration_seconds=0.6,  # 短租期 → watchdog 间隔 0.1s
            batch_size=1,
            provider_registry={"telegram_send": _slow_provider},
        )
        # 直接构造 ev dict 调用 process_event 模拟 impostor 拿到 lease 的场景
        # lease_expires_at = now + 0.1s(< 0.2s 阈值,首次 watchdog 即续租)
        ev = {
            "id": eid,
            "effect_type": "telegram_send",
            "target": "chat:1",
            "request_hash": rh,
            "action_id": "act_watchdog_fail",
            "payload_json": "{}",
            "lease_version": 1,
            "lease_expires_at": (
                datetime.utcnow() + timedelta(seconds=0.1)
            ).isoformat(),
        }
        with pytest.raises(AppError) as exc_info:
            await worker.process_event(ev, real_store)
        assert exc_info.value.code == ErrorCodes.OUTBOX_LEASE_RENEW_FAILED
        # 验证事件未 complete(watchdog 续租失败已停止提交)
        cursor = await real_store._db.execute(
            "SELECT status FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] != "completed"


# ════════════════════════════════════════════════════════════════
# 6. DLQ 闭环(move_outbox_to_dlq 写 dlq_reason/dlq_at + outbox_dlq_audit 审计记录)
# ════════════════════════════════════════════════════════════════

class TestDLQAuditClosure:
    """R64 P0-04: DLQ 必须告警并产生可审批 replay 审计记录。

    move_outbox_to_dlq 同步写入:
    - outbox_events.dlq_reason / dlq_at 审计字段
    - outbox_dlq_audit 审计记录(可审批 replay:pending → approved/rejected/replayed)
    - worker 调用后 logger.error 告警
    """

    @pytest.mark.asyncio
    async def test_move_outbox_to_dlq_writes_dlq_reason_and_at(self, real_store):
        """move_outbox_to_dlq 写入 dlq_reason / dlq_at 审计字段。"""
        rh = "rh_dlq_audit" + "0" * 52
        eid = await real_store.add_outbox_event(
            action_id="act_dlq_audit",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json=json.dumps({"text": "fail"}),
        )
        ok = await real_store.move_outbox_to_dlq(eid, reason="provider_permanent_fail")
        assert ok is True
        cursor = await real_store._db.execute(
            "SELECT status, dlq_reason, dlq_at, last_error "
            "FROM outbox_events WHERE id=?", (eid,),
        )
        row = await cursor.fetchone()
        assert row[0] == "dlq"
        assert row[1] == "provider_permanent_fail"
        assert row[2] is not None and len(row[2]) > 0  # ISO timestamp
        assert row[3] == "provider_permanent_fail"  # last_error 同步写入

    @pytest.mark.asyncio
    async def test_move_outbox_to_dlq_writes_audit_record(self, real_store):
        """move_outbox_to_dlq 写入 outbox_dlq_audit 审计记录(可审批 replay)。"""
        rh = "rh_dlq_audit_rec" + "0" * 48
        eid = await real_store.add_outbox_event(
            action_id="act_dlq_audit_rec",
            effect_type="r2_put",
            target="bucket/key",
            request_hash=rh,
            payload_json=json.dumps({"size": 1024}),
        )
        await real_store.claim_outbox_events(
            lease_owner="worker_dlq", lease_duration_seconds=60, limit=1,
        )
        ok = await real_store.move_outbox_to_dlq(eid, reason="over_max_attempts")
        assert ok is True
        # 验证 outbox_dlq_audit 表写入了一条审计记录
        audit_records = await real_store.get_outbox_dlq_audit(
            replay_status="pending", limit=10,
        )
        assert len(audit_records) >= 1
        rec = next(r for r in audit_records if r["event_id"] == eid)
        assert rec["action_id"] == "act_dlq_audit_rec"
        assert rec["effect_type"] == "r2_put"
        assert rec["target"] == "bucket/key"
        assert rec["request_hash"] == rh
        assert rec["payload_json"] == json.dumps({"size": 1024})
        assert rec["dlq_reason"] == "over_max_attempts"
        assert rec["lease_owner"] == "worker_dlq"
        assert rec["lease_version"] == 1
        assert rec["replay_status"] == "pending"

    @pytest.mark.asyncio
    async def test_outbox_worker_dlq_logs_error_and_writes_audit(self, real_store):
        """worker 调用 move_outbox_to_dlq 后 logger.error 告警 + 审计记录写入。

        场景:provider_registry 不含 effect_type → process_event 调用
        move_outbox_to_dlq,worker logger.error 告警,同时 outbox_dlq_audit 写入。
        """
        from services.data_lifecycle import OutboxEnvelope, OutboxWorker
        from loguru import logger as _loguru_logger

        async def _unused_provider(envelope: OutboxEnvelope):
            return "ext"

        rh = "rh_dlq_worker" + "0" * 51
        eid = await real_store.add_outbox_event(
            action_id="act_dlq_worker",
            effect_type="unknown_effect_xyz",  # registry 不含此 effect_type
            target="chat:1",
            request_hash=rh,
            payload_json=json.dumps({"text": "hi"}),
        )
        worker = OutboxWorker(
            lease_owner="worker_dlq_test", batch_size=10,
            provider_registry={"telegram_send": _unused_provider},
        )
        # 用 loguru 自定义 sink 捕获 ERROR 日志(caplog 不直接支持 loguru)
        captured_logs: list[str] = []
        sink_id = _loguru_logger.add(
            lambda msg: captured_logs.append(msg.record["message"]),
            level="ERROR",
            format="{message}",
        )
        try:
            result = await worker.run_once()
        finally:
            _loguru_logger.remove(sink_id)
        assert result["claimed"] == 1
        assert result["dlq"] == 1
        assert result["completed"] == 0
        # 验证 logger.error 告警(含 DLQ 关键字 + event_id)
        dlq_logs = [m for m in captured_logs if "DLQ" in m]
        assert len(dlq_logs) >= 1
        assert f"event_id={eid}" in dlq_logs[0]
        # 验证 outbox_dlq_audit 审计记录已写入
        audit_records = await real_store.get_outbox_dlq_audit(
            replay_status="pending", limit=10,
        )
        rec = next(r for r in audit_records if r["event_id"] == eid)
        assert rec["effect_type"] == "unknown_effect_xyz"
        assert "no_provider" in rec["dlq_reason"]
        assert rec["replay_status"] == "pending"  # 可审批 replay

    @pytest.mark.asyncio
    async def test_dlq_audit_replay_status_workflow(self, real_store):
        """outbox_dlq_audit 支持 replay 审批流程(pending → approved/rejected/replayed)。"""
        rh = "rh_dlq_workflow" + "0" * 49
        eid = await real_store.add_outbox_event(
            action_id="act_dlq_workflow",
            effect_type="telegram_send",
            target="chat:1",
            request_hash=rh,
            payload_json="{}",
        )
        await real_store.move_outbox_to_dlq(eid, reason="manual_test")
        # 初始 replay_status=pending
        records = await real_store.get_outbox_dlq_audit(
            replay_status="pending", limit=10,
        )
        assert len(records) >= 1
        audit_id = records[0]["id"]
        # 审批通过(approved)
        ok = await real_store.update_outbox_dlq_audit_replay(
            audit_id, replay_status="approved",
            replayed_by="admin_alice", replay_note="approved for replay",
        )
        assert ok is True
        # 验证 pending 中已无此记录
        pending_records = await real_store.get_outbox_dlq_audit(
            replay_status="pending", limit=10,
        )
        assert all(r["id"] != audit_id for r in pending_records)
        # approved 中应有此记录
        approved_records = await real_store.get_outbox_dlq_audit(
            replay_status="approved", limit=10,
        )
        rec = next(r for r in approved_records if r["id"] == audit_id)
        assert rec["replayed_by"] == "admin_alice"
        assert rec["replay_note"] == "approved for replay"
        assert rec["replay_status"] == "approved"


# ── asyncio 引入(用于 TestAutoLeaseRenewal 中的 asyncio.sleep) ──
import asyncio
