"""R50 P0-3: Critical Effect Hash 运行时闭环测试。

被测目标:
- ``services.effect_receipts.build_canonical_effect_params``
- ``services.effect_receipts.compute_effect_request_hash_safe``
- ``services.effect_receipts.EffectReceiptManager.check_receipt``
  (hash mismatch → reconcile_status='hash_mismatch_needs_reconcile' + 特殊返回标记)
- ``services.effect_receipts.EffectReceiptManager.list_pending_reconcile``
  (包含 hash_mismatch_needs_reconcile)
- ``services.effect_receipts.EffectReceiptManager.record_pending`` /
  ``record_completed`` (crash-window 故障注入)

测试场景(13 个):
 1. test_build_canonical_effect_params_stable_order:
    同一组参数不同传入顺序产生相同 params dict + 相同 hash
 2. test_build_canonical_effect_params_no_target_raises:
    critical effect 无 target 标识 raise ValueError
 3. test_build_canonical_effect_params_none_filtered:
    None 值字段不进入 params
 4. test_compute_effect_request_hash_safe_critical_empty_raises:
    critical effect params 为空 raise ValueError
 5. test_compute_effect_request_hash_safe_non_critical_empty_ok:
    非 critical effect 空 params 返回 hash(向后兼容)
 6. test_compute_effect_request_hash_safe_serialization_failure_raises:
    params 含不可序列化对象 raise ValueError
 7. test_check_receipt_hash_mismatch_marks_reconcile_status:
    期望 hash 与 stored 不匹配 → reconcile_status='hash_mismatch_needs_reconcile',
    返回 status='hash_mismatch'
 8. test_check_receipt_hash_mismatch_does_not_return_completed:
    hash mismatch 不返回 completed 状态
 9. test_list_pending_reconcile_includes_hash_mismatch:
    list_pending_reconcile 包含 hash_mismatch_needs_reconcile
10. test_crash_window_telegram_send_kill_before_completed:
    telegram_send 外部成功 + completed 前 kill -9 → 重启后 check_receipt 返回
    pending,reconcile_status='pending'
11. test_crash_window_r2_put_kill_before_completed:
    同上,effect_type='r2_put'
12. test_crash_window_restore_kill_before_completed:
    同上,effect_type='restore'
13. test_crash_window_completed_but_external_failed:
    record_completed 已成功但外部副作用实际失败 → list_pending_reconcile 不应包含
    (status='completed')

测试策略:
- 使用真实 SQLite 临时数据库(隔离生产数据)
- 每个用例前重置 EffectReceiptManager 单例
- crash-window 测试通过"调用 record_pending 后不调用 record_completed"模拟
  进程在 completed 前 kill -9 的场景
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

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

# 被测模块(顶层导入,便于纯函数测试用例直接引用)
from services.effect_receipts import (  # noqa: E402
    CRITICAL_EFFECT_TYPES,
    build_canonical_effect_params,
    compute_effect_request_hash,
    compute_effect_request_hash_safe,
)


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r50_p0_3_test_")
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


@pytest_asyncio.fixture
async def receipt_manager(real_store):
    """初始化 EffectReceiptManager 单例并返回,用例间隔离。"""
    from services import effect_receipts as _er_mod
    _er_mod._receipt_manager = None
    mgr = _er_mod.get_receipt_manager(real_store)
    yield mgr
    _er_mod._receipt_manager = None
    if real_store._db:
        await real_store._db.execute("DELETE FROM effect_receipts")
        await real_store._db.commit()


@pytest_asyncio.fixture
async def clean_tables(real_store):
    """每个用例前清空 effect_receipts 表。"""
    await real_store._db.execute("DELETE FROM effect_receipts")
    await real_store._db.commit()
    yield real_store
    await real_store._db.execute("DELETE FROM effect_receipts")
    await real_store._db.commit()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _get_reconcile_status(
    store, action_id: str, effect_type: str, target: str,
) -> Optional[str]:
    """查询 effect_receipts 表中的 reconcile_status 字段。"""
    cursor = await store._db.execute(
        "SELECT reconcile_status FROM effect_receipts "
        "WHERE action_id = ? AND effect_type = ? AND target = ?",
        (action_id, effect_type, target),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _get_status(
    store, action_id: str, effect_type: str, target: str,
) -> Optional[str]:
    """查询 effect_receipts 表中的 status 字段。"""
    cursor = await store._db.execute(
        "SELECT status FROM effect_receipts "
        "WHERE action_id = ? AND effect_type = ? AND target = ?",
        (action_id, effect_type, target),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _get_last_error(
    store, action_id: str, effect_type: str, target: str,
) -> Optional[str]:
    """查询 effect_receipts 表中的 last_error 字段。"""
    cursor = await store._db.execute(
        "SELECT last_error FROM effect_receipts "
        "WHERE action_id = ? AND effect_type = ? AND target = ?",
        (action_id, effect_type, target),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _seed_completed_receipt(
    store, action_id: str, effect_type: str, target: str,
    external_id: str = "msg_42",
    request_hash: str = "seed_hash_xxx",
):
    """预置一条 status='completed' 的 receipt(模拟崩溃前已完成的场景)。

    使用传入的 request_hash 直接写入(便于构造 mismatch 场景)。
    """
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    await store._db.execute(
        "INSERT OR REPLACE INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, created_at, "
        " completed_at, request_hash, attempt, reconcile_status) "
        "VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, 1, 'completed')",
        (action_id, effect_type, target, external_id, now, now, request_hash),
    )
    await store._db.commit()


# ════════════════════════════════════════════════════════════════
# 1. build_canonical_effect_params 单元测试(纯函数,无需 DB)
# ════════════════════════════════════════════════════════════════

class TestBuildCanonicalEffectParams:
    """build_canonical_effect_params 单元测试。"""

    def test_build_canonical_effect_params_stable_order(self):
        """测试 1:同一组参数不同传入顺序产生相同 params dict + 相同 hash。"""
        # 顺序 1
        p1 = build_canonical_effect_params(
            "telegram_send",
            chat_id=42,
            text="hi",
            target_user_id=100,
        )
        # 顺序 2(完全相反的传入顺序)
        p2 = build_canonical_effect_params(
            "telegram_send",
            target_user_id=100,
            text="hi",
            chat_id=42,
        )
        # 内容等价
        assert p1 == p2
        # 关键:hash 一致(确保 sort_keys + 稳定构造消除顺序差异)
        h1 = compute_effect_request_hash("telegram_send", p1)
        h2 = compute_effect_request_hash("telegram_send", p2)
        assert h1 == h2
        # hash 是 64 字符 SHA256
        assert len(h1) == 64

    def test_build_canonical_effect_params_no_target_raises(self):
        """测试 2:critical effect 无 target 标识 raise ValueError。"""
        with pytest.raises(ValueError, match="target"):
            build_canonical_effect_params(
                "telegram_send",
                text="hi",
                message_id=1,
                # 不传任何 target_*
            )
        # 也验证只传非 target 字段同样 raise
        with pytest.raises(ValueError, match="target"):
            build_canonical_effect_params(
                "r2_put",
                file_id="abc",
                key="k1",
            )

    def test_build_canonical_effect_params_none_filtered(self):
        """测试 3:None 值字段不进入 params。"""
        p = build_canonical_effect_params(
            "r2_put",
            target_channel_id=42,
            file_id=None,         # 应被过滤
            key=None,             # 应被过滤
            message_id=None,      # 应被过滤
            resource_version="v1",
            text=None,            # 应被过滤
            extra={"a": None, "b": 1, "c": "x"},  # a 应被过滤
        )
        # 只包含非 None 值
        assert p == {
            "target_channel_id": 42,
            "resource_version": "v1",
            "b": 1,
            "c": "x",
        }
        # 显式断言 None 字段不在结果中
        assert "file_id" not in p
        assert "key" not in p
        assert "message_id" not in p
        assert "text" not in p
        assert "a" not in p


# ════════════════════════════════════════════════════════════════
# 2. compute_effect_request_hash_safe 单元测试(纯函数,无需 DB)
# ════════════════════════════════════════════════════════════════

class TestComputeEffectRequestHashSafe:
    """compute_effect_request_hash_safe 单元测试。"""

    def test_compute_effect_request_hash_safe_critical_empty_raises(self):
        """测试 4:critical effect params is empty raise ValueError。"""
        # None
        with pytest.raises(ValueError, match="params is empty"):
            compute_effect_request_hash_safe("telegram_send", None)
        # 空 dict
        with pytest.raises(ValueError, match="params is empty"):
            compute_effect_request_hash_safe("telegram_send", {})
        # 验证所有 critical effect_type 都拒绝空 params
        for et in CRITICAL_EFFECT_TYPES:
            with pytest.raises(ValueError, match="params is empty"):
                compute_effect_request_hash_safe(et, None)

    def test_compute_effect_request_hash_safe_non_critical_empty_ok(self):
        """测试 5:非 critical effect 空 params 返回 hash(向后兼容)。"""
        # None
        h1 = compute_effect_request_hash_safe("r2_upload", None)
        assert isinstance(h1, str)
        assert len(h1) == 64
        # 空 dict
        h2 = compute_effect_request_hash_safe("r2_upload", {})
        assert h1 == h2
        # 与 compute_effect_request_hash 空 params 一致(向后兼容)
        h3 = compute_effect_request_hash("r2_upload", {})
        assert h1 == h3

    def test_compute_effect_request_hash_safe_serialization_failure_raises(self):
        """测试 6:params 含不可序列化对象 raise ValueError。

        default=str 会调用 obj.__str__(),这里用 __str__ 抛 ValueError 的
        极端自定义类触发序列化失败,验证 critical effect 不会降级为空 hash。
        """

        class _Unserializable:
            """__str__ 抛异常,导致 json.dumps(default=str) 失败。"""

            def __str__(self):
                raise ValueError("forced serialization failure")

        bad_params = {"chat_id": 42, "obj": _Unserializable()}
        with pytest.raises(ValueError, match="serialization failed"):
            compute_effect_request_hash_safe("telegram_send", bad_params)

        # 非 critical effect 应兜底为 effect_type-only hash(不 raise)
        h_fallback = compute_effect_request_hash_safe("r2_upload", bad_params)
        assert isinstance(h_fallback, str)
        assert len(h_fallback) == 64
        # 兜底 hash 等价于空 params hash
        assert h_fallback == compute_effect_request_hash("r2_upload", {})


# ════════════════════════════════════════════════════════════════
# 3. check_receipt hash mismatch → DLQ 测试(需要 DB)
# ════════════════════════════════════════════════════════════════

class TestCheckReceiptHashMismatch:
    """check_receipt hash mismatch 行为测试。"""

    @pytest.mark.asyncio
    async def test_check_receipt_hash_mismatch_marks_reconcile_status(
        self, receipt_manager, clean_tables,
    ):
        """测试 7:期望 hash 与 stored 不匹配 → reconcile_status 标记为
        'hash_mismatch_needs_reconcile',返回 status='hash_mismatch'。
        """
        store = clean_tables
        # 预置一条 completed receipt,request_hash = "stored_hash_xxx"
        await _seed_completed_receipt(
            store, "act_mismatch_1", "telegram_send", "chat:42",
            external_id="msg_42",
            request_hash="stored_hash_xxx_yyy_zzz",
        )
        # 调用 check_receipt,传入不同的 expected_request_hash
        result = await receipt_manager.check_receipt(
            "act_mismatch_1", "telegram_send", "chat:42",
            expected_request_hash="expected_hash_aaa_bbb_ccc",
        )
        # 验证返回特殊标记
        assert result is not None
        assert result["status"] == "hash_mismatch"
        assert result["reconcile_status"] == "hash_mismatch_needs_reconcile"
        assert result["external_id"] == "msg_42"
        assert result["request_hash"] == "stored_hash_xxx_yyy_zzz"
        # 验证 DB 中 reconcile_status 已更新
        db_status = await _get_reconcile_status(
            store, "act_mismatch_1", "telegram_send", "chat:42",
        )
        assert db_status == "hash_mismatch_needs_reconcile"
        # last_error 含 hash_mismatch 标记
        last_err = await _get_last_error(
            store, "act_mismatch_1", "telegram_send", "chat:42",
        )
        assert last_err is not None
        assert "hash_mismatch" in last_err

    @pytest.mark.asyncio
    async def test_check_receipt_hash_mismatch_does_not_return_completed(
        self, receipt_manager, clean_tables,
    ):
        """测试 8:hash mismatch 不返回 completed 状态。

        调用方不应误以为副作用已完成而跳过,也不应直接重试(应进入 DLQ)。
        """
        store = clean_tables
        await _seed_completed_receipt(
            store, "act_mismatch_2", "r2_put", "key:abc",
            external_id="r2_obj_1",
            request_hash="stored_r2_hash",
        )
        result = await receipt_manager.check_receipt(
            "act_mismatch_2", "r2_put", "key:abc",
            expected_request_hash="expected_r2_hash_different",
        )
        assert result is not None
        # 关键:status 不是 'completed'
        assert result["status"] != "completed"
        assert result["status"] == "hash_mismatch"
        # DB 中 status 列仍为 completed(只有 reconcile_status 被标记),
        # 但调用方通过返回值的 status 字段区分,不会被误导为 completed
        db_row_status = await _get_status(
            store, "act_mismatch_2", "r2_put", "key:abc",
        )
        # DB 中 status 列保持 completed(未被改动),但 reconcile_status 已标记
        assert db_row_status == "completed"
        db_reconcile = await _get_reconcile_status(
            store, "act_mismatch_2", "r2_put", "key:abc",
        )
        assert db_reconcile == "hash_mismatch_needs_reconcile"


# ════════════════════════════════════════════════════════════════
# 4. list_pending_reconcile 包含 hash_mismatch 测试
# ════════════════════════════════════════════════════════════════

class TestListPendingReconcileIncludesHashMismatch:
    """list_pending_reconcile 包含 hash_mismatch_needs_reconcile 测试。"""

    @pytest.mark.asyncio
    async def test_list_pending_reconcile_includes_hash_mismatch(
        self, receipt_manager, clean_tables,
    ):
        """测试 9:list_pending_reconcile 同时包含 needs_reconcile 和
        hash_mismatch_needs_reconcile 两类 receipt。
        """
        store = clean_tables
        import datetime
        now = datetime.datetime.utcnow().isoformat()

        # 行 A:reconcile_status='needs_reconcile'(执行失败)
        await store._db.execute(
            "INSERT INTO effect_receipts "
            "(action_id, effect_type, target, status, external_id, created_at, "
            " request_hash, attempt, reconcile_status, last_error) "
            "VALUES (?, ?, ?, 'failed', NULL, ?, ?, 1, 'needs_reconcile', 'boom')",
            ("act_a", "telegram_send", "chat:1", now, "hash_a"),
        )
        # 行 B:reconcile_status='hash_mismatch_needs_reconcile'
        await store._db.execute(
            "INSERT INTO effect_receipts "
            "(action_id, effect_type, target, status, external_id, created_at, "
            " request_hash, attempt, reconcile_status, last_error) "
            "VALUES (?, ?, ?, 'completed', 'ext_b', ?, ?, 1, "
            " 'hash_mismatch_needs_reconcile', 'hash_mismatch: ...')",
            ("act_b", "r2_put", "key:b", now, "hash_b"),
        )
        # 行 C:reconcile_status='completed'(不应出现)
        await store._db.execute(
            "INSERT INTO effect_receipts "
            "(action_id, effect_type, target, status, external_id, created_at, "
            " request_hash, attempt, reconcile_status) "
            "VALUES (?, ?, ?, 'completed', 'ext_c', ?, ?, 1, 'completed')",
            ("act_c", "restore", "key:c", now, "hash_c"),
        )
        # 行 D:reconcile_status=NULL(不应出现)
        await store._db.execute(
            "INSERT INTO effect_receipts "
            "(action_id, effect_type, target, status, external_id, created_at, "
            " request_hash, attempt, reconcile_status) "
            "VALUES (?, ?, ?, 'pending', NULL, ?, ?, 1, NULL)",
            ("act_d", "ban", "user:d", now, "hash_d"),
        )
        await store._db.commit()

        results = await receipt_manager.list_pending_reconcile(limit=100)
        action_ids = {r["action_id"] for r in results}

        # 包含 A(needs_reconcile)和 B(hash_mismatch_needs_reconcile)
        assert "act_a" in action_ids
        assert "act_b" in action_ids
        # 不包含 C(completed)和 D(NULL)
        assert "act_c" not in action_ids
        assert "act_d" not in action_ids

        # 验证 act_b 的 reconcile_status 字段
        act_b = next(r for r in results if r["action_id"] == "act_b")
        assert act_b["reconcile_status"] == "hash_mismatch_needs_reconcile"
        assert act_b["effect_type"] == "r2_put"
        assert act_b["target"] == "key:b"


# ════════════════════════════════════════════════════════════════
# 5. crash-window 故障注入测试
# ════════════════════════════════════════════════════════════════

class TestCrashWindowFaultInjection:
    """crash-window 故障注入测试。

    模拟场景:外部副作用已成功,但进程在 record_completed 之前 kill -9。
    重启后 check_receipt 应返回 pending,reconcile_status='pending'。
    """

    @pytest.mark.asyncio
    async def test_crash_window_telegram_send_kill_before_completed(
        self, receipt_manager, clean_tables,
    ):
        """测试 10:telegram_send 外部成功 + completed 前 kill -9。

        场景:
        1. record_pending 成功(claim receipt)
        2. 外部 telegram API 调用成功(message_id=999)
        3. 进程在 record_completed 之前 kill -9(模拟崩溃)
        4. 重启后 check_receipt 应返回 None(pending 不视为 completed),
           DB 中 status='pending', reconcile_status='pending'
        """
        store = clean_tables
        # 构造 canonical params + request_hash
        params = build_canonical_effect_params(
            "telegram_send",
            chat_id=42,
            text="crash_test",
        )
        request_hash = compute_effect_request_hash_safe(
            "telegram_send", params,
        )

        # 步骤 1:record_pending 成功(模拟副作用开始执行)
        claimed = await receipt_manager.record_pending(
            "act_crash_tg", "telegram_send", "chat:42",
            request_hash=request_hash,
            lease_owner="worker_1",
            lease_until="2099-01-01",
            fail_closed=True,
        )
        assert claimed is True

        # 步骤 2:外部 telegram API 成功(本测试不实际调用,用变量模拟)
        _external_message_id = 999  # 外部成功返回的 message_id

        # 步骤 3:进程 kill -9(不调用 record_completed,直接模拟重启)
        # —— 故障注入点 ——

        # 步骤 4:重启后 check_receipt
        result = await receipt_manager.check_receipt(
            "act_crash_tg", "telegram_send", "chat:42",
            expected_request_hash=request_hash,
        )
        # pending 不视为 completed → 返回 None
        assert result is None

        # 验证 DB 中 status='pending', reconcile_status='pending'
        db_status = await _get_status(
            store, "act_crash_tg", "telegram_send", "chat:42",
        )
        assert db_status == "pending"
        db_reconcile = await _get_reconcile_status(
            store, "act_crash_tg", "telegram_send", "chat:42",
        )
        assert db_reconcile == "pending"

        # 验证重启后调用方可重新 claim(record_pending 返回 True)
        reclaimed = await receipt_manager.record_pending(
            "act_crash_tg", "telegram_send", "chat:42",
            request_hash=request_hash,
            lease_owner="worker_2",
            lease_until="2099-01-01",
            fail_closed=True,
        )
        assert reclaimed is True

        # 验证 attempt 增加(从 1 → 2)
        cursor = await store._db.execute(
            "SELECT attempt FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=?",
            ("act_crash_tg", "telegram_send", "chat:42"),
        )
        row = await cursor.fetchone()
        assert row[0] == 2

    @pytest.mark.asyncio
    async def test_crash_window_r2_put_kill_before_completed(
        self, receipt_manager, clean_tables,
    ):
        """测试 11:r2_put 外部成功 + completed 前 kill -9。"""
        store = clean_tables
        params = build_canonical_effect_params(
            "r2_put",
            target_channel_id=42,
            key="backup/v1/file.bak",
            resource_version="file_code_abc_v3",
        )
        request_hash = compute_effect_request_hash_safe("r2_put", params)

        # record_pending 成功
        claimed = await receipt_manager.record_pending(
            "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
            request_hash=request_hash,
            lease_owner="worker_r2",
            lease_until="2099-01-01",
            fail_closed=True,
        )
        assert claimed is True

        # 外部 R2 PUT 成功(模拟)
        _r2_object_key = "backup/v1/file.bak"

        # —— 进程 kill -9(不调用 record_completed)——

        # 重启后 check_receipt
        result = await receipt_manager.check_receipt(
            "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
            expected_request_hash=request_hash,
        )
        assert result is None  # pending 不视为 completed

        # DB 中 status='pending'
        db_status = await _get_status(
            store, "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
        )
        assert db_status == "pending"
        db_reconcile = await _get_reconcile_status(
            store, "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
        )
        assert db_reconcile == "pending"

        # 重启后可重新 claim + 完成
        await receipt_manager.record_pending(
            "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
            request_hash=request_hash,
            fail_closed=True,
        )
        await receipt_manager.record_completed(
            "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
            external_id="r2_etag_xyz",
            expected_request_hash=request_hash,
            fail_closed=True,
        )
        # 现在 check_receipt 应返回 completed
        result_after = await receipt_manager.check_receipt(
            "act_crash_r2", "r2_put", "key:backup/v1/file.bak",
            expected_request_hash=request_hash,
        )
        assert result_after is not None
        assert result_after["status"] == "completed"
        assert result_after["external_id"] == "r2_etag_xyz"

    @pytest.mark.asyncio
    async def test_crash_window_restore_kill_before_completed(
        self, receipt_manager, clean_tables,
    ):
        """测试 12:restore 外部成功 + completed 前 kill -9。"""
        store = clean_tables
        params = build_canonical_effect_params(
            "restore",
            target_user_id=100,
            key="backup/v2/restore.bak",
            resource_version="file_code_def_v2",
        )
        request_hash = compute_effect_request_hash_safe("restore", params)

        # record_pending 成功
        claimed = await receipt_manager.record_pending(
            "act_crash_restore", "restore", "key:backup/v2/restore.bak",
            request_hash=request_hash,
            lease_owner="worker_restore",
            lease_until="2099-01-01",
            fail_closed=True,
        )
        assert claimed is True

        # 外部 restore 成功(模拟)
        _restored_rows = 42

        # —— 进程 kill -9(不调用 record_completed)——

        # 重启后 check_receipt
        result = await receipt_manager.check_receipt(
            "act_crash_restore", "restore", "key:backup/v2/restore.bak",
            expected_request_hash=request_hash,
        )
        assert result is None  # pending 不视为 completed

        # DB 中 status='pending'
        db_status = await _get_status(
            store, "act_crash_restore", "restore", "key:backup/v2/restore.bak",
        )
        assert db_status == "pending"
        db_reconcile = await _get_reconcile_status(
            store, "act_crash_restore", "restore", "key:backup/v2/restore.bak",
        )
        assert db_reconcile == "pending"

        # 验证 list_pending_reconcile 不包含 pending 行(只包含 needs_reconcile/
        # hash_mismatch_needs_reconcile);pending 表示正在执行,尚未失败
        reconcile_list = await receipt_manager.list_pending_reconcile(limit=100)
        action_ids = {r["action_id"] for r in reconcile_list}
        assert "act_crash_restore" not in action_ids

    @pytest.mark.asyncio
    async def test_crash_window_completed_but_external_failed(
        self, receipt_manager, clean_tables,
    ):
        """测试 13:record_completed 已成功但外部副作用实际失败。

        场景:RTO 后发现 record_completed 已写入(可能因网络抖动 external_id 不准),
        但实际外部副作用失败(如 telegram 返回了 message_id 但消息被风控撤回)。
        此时应进入人工 reconcile 流程,但 list_pending_reconcile 不应自动包含
        (因为 status='completed', reconcile_status='completed'),
        需要外部巡检任务主动标记为 needs_reconcile 才会进入 DLQ。
        """
        store = clean_tables
        params = build_canonical_effect_params(
            "telegram_send",
            chat_id=99,
            text="rto_test",
        )
        request_hash = compute_effect_request_hash_safe(
            "telegram_send", params,
        )

        # record_pending → record_completed(完整成功路径)
        await receipt_manager.record_pending(
            "act_rto", "telegram_send", "chat:99",
            request_hash=request_hash,
            fail_closed=True,
        )
        await receipt_manager.record_completed(
            "act_rto", "telegram_send", "chat:99",
            external_id="msg_999",
            expected_request_hash=request_hash,
            fail_closed=True,
        )

        # 验证 DB 中 status='completed', reconcile_status='completed'
        db_status = await _get_status(
            store, "act_rto", "telegram_send", "chat:99",
        )
        assert db_status == "completed"
        db_reconcile = await _get_reconcile_status(
            store, "act_rto", "telegram_send", "chat:99",
        )
        assert db_reconcile == "completed"

        # list_pending_reconcile 不应包含(因为 reconcile_status='completed')
        reconcile_list = await receipt_manager.list_pending_reconcile(limit=100)
        action_ids = {r["action_id"] for r in reconcile_list}
        assert "act_rto" not in action_ids

        # 假设外部巡检发现 message_id=999 实际已被风控撤回,
        # 主动标记为 needs_reconcile(模拟外部巡检任务的行为)
        await store._db.execute(
            "UPDATE effect_receipts "
            "SET reconcile_status='needs_reconcile', "
            "last_error='external message revoked by risk control' "
            "WHERE action_id=? AND effect_type=? AND target=?",
            ("act_rto", "telegram_send", "chat:99"),
        )
        await store._db.commit()

        # 现在 list_pending_reconcile 应包含(被人工/巡检标记)
        reconcile_list_after = await receipt_manager.list_pending_reconcile(
            limit=100,
        )
        action_ids_after = {r["action_id"] for r in reconcile_list_after}
        assert "act_rto" in action_ids_after

        act_rto = next(
            r for r in reconcile_list_after if r["action_id"] == "act_rto"
        )
        assert act_rto["status"] == "completed"  # status 列未变
        assert act_rto["reconcile_status"] == "needs_reconcile"
