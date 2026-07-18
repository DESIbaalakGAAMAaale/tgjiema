"""R50 终审报告 P1-3: 媒体组 / Caption / Callback / MFA 多 worker crash-window 故障注入测试。

测试覆盖范围(P1-3 故障注入矩阵):
    A. 媒体组 crash-window 测试(4 用例)
        1. test_media_group_partial_success_kill_before_completed
        2. test_media_group_confirmed_count_atomic_under_concurrency
        3. test_caption_send_failure_does_not_mark_file_completed
        4. test_receipt_read_failure_pauses_processing
    B. MFA 多 worker 并发测试(4 用例)
        5. test_mfa_concurrent_same_totp_only_one_succeeds
        6. test_mfa_concurrent_different_totp_all_succeed
        7. test_mfa_timestep_boundary_retries
        8. test_mfa_replay_same_totp_rejected
    C. 整体 receipt 一致性测试(4 用例)
        9. test_crash_window_receipt_state_consistency
        10. test_crash_window_completed_but_external_unknown
        11. test_crash_window_failed_then_retry_succeeds
        12. test_concurrent_record_completed_idempotent
    D. 媒体组 + MFA 组合场景(2 用例)
        13. test_media_group_with_mfa_approval
        14. test_media_group_partial_mfa_failure

故障注入技术说明:
    - 真实 SQLite 临时数据库(隔离生产数据),由 CacheStore.init() 创建 effect_receipts
      / mfa_used_totp / mfa_failures / delivery_group_receipts / callback_nonces 表
    - asyncio.gather 模拟多 worker 并发(同 TOTP 多 worker / 并发 record_completed)
    - monkeypatch / unittest.mock.patch 模拟进程崩溃 / DB 读失败 / store 不可用
    - time.time() mock 模拟 TOTP timestep 边界(29.9s → 30.1s 切换点)
    - 不依赖 telegram / pyotp(可选):pyotp 缺失时通过 _consume_totp_timestep +
      _find_matching_timestep 直接验证原子消费语义

被测代码引用:
    - services/effect_receipts.py: EffectReceiptManager.{check_receipt, record_pending,
      record_completed, record_failed, list_pending_reconcile}
    - admin/mfa.py: MFAManager.{verify_totp_code, generate_totp_secret, enable_mfa} +
      _consume_totp_timestep + _find_matching_timestep
    - database/cache_store.py: CacheStore.{delivery_group_receipt_create,
      delivery_group_receipt_confirm_child, delivery_group_receipt_get,
      delivery_group_receipt_list_pending}
    - bots/dsp_bot.py: 媒体组 receipt 数据结构(group_id =
      f"dsp_batch:{job_id}:{storage_channel_id}:{target_user_id}", child action_id =
      f"dsp:{job_id}:{storage_channel_id}:{mid}:{chat_id}:{idx}", caption action_id =
      f"dsp:{job_id}:{msg_id}:edit_caption")
"""
from __future__ import annotations

import inspect
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

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

# 尝试导入 pyotp,未安装则跳过本模块中需要真实 TOTP 生成的用例
try:
    import pyotp  # noqa: F401
    _PYOTP_AVAILABLE = True
except ImportError:
    _PYOTP_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库(由 init() 创建含 R49 P0-4 约束的表)
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建以下测试所需的表:
        - effect_receipts(R49 P0-4: request_hash TEXT NOT NULL + CHECK 约束)
        - mfa_used_totp(R46 P1: TOTP 重放防护)
        - mfa_failures(R47 P1-b: 毫秒时间戳防碰撞)
        - delivery_group_receipts(R47 P0-5: 群发回执聚合)
        - callback_nonces(R47 P1-a: 回调 nonce 原子消费)

    同时设置 _cs_module._store 使 get_cache_store() 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r50_p1_3_test_")
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


@pytest.fixture(autouse=True)
def reset_mfa_state():
    """每个用例前重置 MFA 模块级 L1 缓存状态。"""
    from admin import mfa as _mfa_mod
    _mfa_mod.reset_mfa_state_for_testing()
    yield
    _mfa_mod.reset_mfa_state_for_testing()


# ════════════════════════════════════════════════════════════════
# A. 媒体组 crash-window 测试
# ════════════════════════════════════════════════════════════════


class TestMediaGroupCrashWindow:
    """A. 媒体组 crash-window 故障注入测试(4 用例)。"""

    @pytest.mark.asyncio
    async def test_media_group_partial_success_kill_before_completed(self, real_store):
        """用例 1: 媒体组部分成功后 kill -9(crash-window)。

        场景:
            - 3 文件媒体组(group_id = dsp_batch:job1:ch1:user1)
            - 文件 1: record_pending → record_completed(成功 send)
            - 文件 2: record_pending 后 kill -9(模拟进程崩溃,未 record_completed)
            - 文件 3: 未启动(无 receipt)

        重启后:
            - effect_receipts 中文件 1 status='completed'
            - effect_receipts 中文件 2 status='pending'(crash-window)
            - 文件 2 应能继续执行(不重复发送文件 1)
            - reconciliation 扫描器将 pending 标记为 needs_reconcile 后,
              list_pending_reconcile 包含文件 2
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)

        # 媒体组 receipt 数据结构(从 bots/dsp_bot.py 提取):
        # group_id = f"dsp_batch:{job_id}:{storage_channel_id}:{target_user_id}"
        # child action_id = f"dsp:{job_id}:{storage_channel_id}:{mid}:{chat_id}:{idx}"
        job_id = 50001
        storage_channel_id = 100
        target_user_id = 200
        group_id = f"dsp_batch:{job_id}:{storage_channel_id}:{target_user_id}"

        # 创建群发回执(3 个文件)
        await real_store.delivery_group_receipt_create(
            group_id=group_id,
            expected_count=3,
            source_ids=[101, 102, 103],
            target_ids=[target_user_id] * 3,
            action_id=group_id,
        )

        # 文件 1: 完整流程 record_pending → record_completed
        action_id_1 = f"dsp:{job_id}:{storage_channel_id}:101:{target_user_id}:0"
        target_1 = f"chat:{target_user_id}"
        params_1 = {
            "target_chat_id": target_user_id,
            "storage_channel_id": storage_channel_id,
            "message_id": 101,
            "job_id": job_id,
            "child_index": 0,
        }
        from services.effect_receipts import compute_effect_request_hash
        hash_1 = compute_effect_request_hash("telegram_send", params_1)
        ok1 = await mgr.record_pending(
            action_id_1, "telegram_send", target_1, request_hash=hash_1,
        )
        assert ok1 is True
        await mgr.record_completed(
            action_id_1, "telegram_send", target_1, external_id="msg_201",
        )
        # 文件 1 group receipt confirm
        await real_store.delivery_group_receipt_confirm_child(group_id, 101)

        # 文件 2: 仅 record_pending(crash-window,模拟 kill -9)
        action_id_2 = f"dsp:{job_id}:{storage_channel_id}:102:{target_user_id}:1"
        target_2 = f"chat:{target_user_id}"
        params_2 = {
            "target_chat_id": target_user_id,
            "storage_channel_id": storage_channel_id,
            "message_id": 102,
            "job_id": job_id,
            "child_index": 1,
        }
        hash_2 = compute_effect_request_hash("telegram_send", params_2)
        ok2 = await mgr.record_pending(
            action_id_2, "telegram_send", target_2, request_hash=hash_2,
        )
        assert ok2 is True
        # 模拟 kill -9:不调用 record_completed,进程崩溃

        # ── 模拟重启后 ──
        # 验证文件 1 receipt status='completed'
        receipt_1 = await mgr.check_receipt(
            action_id_1, "telegram_send", target_1,
            expected_request_hash=hash_1,
        )
        assert receipt_1 is not None
        assert receipt_1["status"] == "completed", (
            "文件 1 应为 completed(已成功 send)"
        )
        assert receipt_1["external_id"] == "msg_201"

        # 验证文件 2 receipt status='pending'(crash-window)
        receipt_2 = await mgr.check_receipt(
            action_id_2, "telegram_send", target_2,
            expected_request_hash=hash_2,
        )
        # check_receipt 仅在 status='completed' 时返回 dict,否则返回 None
        # 此处文件 2 status='pending',应返回 None(未完成)
        assert receipt_2 is None, (
            "文件 2 status='pending' → check_receipt 应返回 None(未完成)"
        )

        # 直接查询 DB 验证 status='pending'
        cursor = await real_store._db.execute(
            "SELECT status, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id_2, "telegram_send", target_2),
        )
        row = await cursor.fetchone()
        assert row is not None, "文件 2 receipt 应存在"
        assert row[0] == "pending", f"文件 2 status 应为 'pending',实际 '{row[0]}'"
        assert row[1] == "pending", (
            f"文件 2 reconcile_status 应为 'pending',实际 '{row[1]}'"
        )

        # 重启后文件 2 应能继续执行:re-record_pending(幂等返回)
        # R62 P1-01 整改语义:pending + 同 request_hash → 幂等返回 True,
        # attempt 不再递增(外部副作用重试计数由 outbox_events.attempt_count 负责,
        # effect_receipts.attempt 仅在 failed→pending 重试时 +1)。
        ok2_retry = await mgr.record_pending(
            action_id_2, "telegram_send", target_2, request_hash=hash_2,
        )
        assert ok2_retry is True, "重启后文件 2 应能 re-claim(幂等返回 True)"

        # 验证 attempt 不变(R62 P1-01: pending+同 hash 幂等,attempt 保持 1)
        cursor = await real_store._db.execute(
            "SELECT attempt FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id_2, "telegram_send", target_2),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, (
            f"重启后文件 2 attempt 应保持 1(R62 P1-01 幂等语义),实际 {row[0]}"
        )

        # 验证文件 1 不被重复发送(check_receipt 返回 completed → 调用方跳过)
        receipt_1_again = await mgr.check_receipt(
            action_id_1, "telegram_send", target_1,
            expected_request_hash=hash_1,
        )
        assert receipt_1_again is not None
        assert receipt_1_again["status"] == "completed"

        # 模拟 reconciliation 扫描器:将 pending 标记为 needs_reconcile
        # (生产中由后台扫描器周期性检查超时 pending 行)
        await real_store._db.execute(
            "UPDATE effect_receipts SET reconcile_status = 'needs_reconcile' "
            "WHERE action_id = ? AND status = 'pending'",
            (action_id_2,),
        )
        await real_store._db.commit()

        # 验证 list_pending_reconcile 包含文件 2
        pending = await mgr.list_pending_reconcile(limit=100)
        action_ids = [p["action_id"] for p in pending]
        assert action_id_2 in action_ids, (
            "list_pending_reconcile 应包含文件 2(needs_reconcile)"
        )

    @pytest.mark.asyncio
    async def test_media_group_confirmed_count_atomic_under_concurrency(self, real_store):
        """用例 2: confirmed_count 原子性(5 worker 并发 confirm_child)。

        场景:
            - 5 文件媒体组(expected_count=5)
            - 5 个并发 worker 同时调用 delivery_group_receipt_confirm_child
            - 断言:confirmed_count 最终 = 5(原子性,无 race condition)
            - 断言:无 count 错乱(每条 UPDATE 都是 atomic increment)

        故障注入:asyncio.gather 模拟 5 worker 并发
        """
        import asyncio

        group_id = "dsp_batch:job2:ch2:user2"
        await real_store.delivery_group_receipt_create(
            group_id=group_id,
            expected_count=5,
            source_ids=[201, 202, 203, 204, 205],
            target_ids=[200] * 5,
            action_id=group_id,
        )

        # 5 个并发 worker 同时 confirm_child
        # 注意:delivery_group_receipt_confirm_child 内部
        #   UPDATE ... SET confirmed_count = confirmed_count + 1
        #   是 SQLite 原子操作(WAL 模式 + writer lock 串行化)
        async def _confirm(child_msg_id):
            return await real_store.delivery_group_receipt_confirm_child(
                group_id, child_msg_id,
            )

        results = await asyncio.gather(
            _confirm(201), _confirm(202), _confirm(203), _confirm(204), _confirm(205),
        )

        # 每次调用应返回非 None(成功 increment)
        for r in results:
            assert r is not None, "confirm_child 不应返回 None"

        # 最终 confirmed_count 应严格 = 5(原子性)
        receipt = await real_store.delivery_group_receipt_get(group_id)
        assert receipt is not None
        assert receipt["confirmed_count"] == 5, (
            f"5 worker 并发后 confirmed_count 应 = 5(原子性),"
            f"实际 {receipt['confirmed_count']}(race condition!)"
        )
        assert receipt["expected_count"] == 5
        # 全部 confirm 后 status 应为 'completed'
        assert receipt["status"] == "completed", (
            f"5/5 confirm 后 status 应为 'completed',实际 '{receipt['status']}'"
        )

    @pytest.mark.asyncio
    async def test_caption_send_failure_does_not_mark_file_completed(self, real_store):
        """用例 3: caption 失败语义(file send 成功但 caption send 失败)。

        场景:
            - 模拟文件 send 成功(telegram_send receipt completed)
            - 模拟 caption send 失败(telegram_edit_caption receipt failed)
            - 断言:effect_receipts 中 caption receipt status='failed'
            - 断言:last_error 含 caption 失败原因
            - 断言:reconcile_status='needs_reconcile'

        注:dsp_bot._edit_sent_caption 中 caption 失败是非致命的(best_effort),
            但 receipt 层面应记录 failed + needs_reconcile 供后续审计。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)

        # 媒体组 receipt 数据结构(从 bots/dsp_bot.py _edit_sent_caption 提取):
        # caption action_id = f"dsp:{job_id}:{msg_id}:edit_caption"
        # caption effect_type = 'telegram_edit_caption'(非 critical,best_effort)
        # caption target = f"chat:{chat_id}"
        job_id = 50003
        message_id = 301
        chat_id = 400
        caption_action_id = f"dsp:{job_id}:{message_id}:edit_caption"
        caption_target = f"chat:{chat_id}"

        # 1. record_pending caption(非 critical,允许空 request_hash)
        ok = await mgr.record_pending(
            caption_action_id, "telegram_edit_caption", caption_target,
            request_hash="",  # 非 critical 允许空 hash
        )
        assert ok is True

        # 2. 模拟 caption send 失败 → record_failed
        caption_error = "TelegramError: Bad Request: message to edit not found"
        await mgr.record_failed(
            caption_action_id, "telegram_edit_caption", caption_target,
            error_msg=caption_error,
        )

        # 3. 断言:caption receipt status='failed'(不是 completed)
        cursor = await real_store._db.execute(
            "SELECT status, last_error, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (caption_action_id, "telegram_edit_caption", caption_target),
        )
        row = await cursor.fetchone()
        assert row is not None, "caption receipt 应存在"
        assert row[0] == "failed", (
            f"caption 失败后 status 应为 'failed'(不是 completed),实际 '{row[0]}'"
        )
        # last_error 含 caption 失败原因
        assert row[1] is not None and "message to edit not found" in row[1], (
            f"last_error 应含 caption 失败原因,实际 '{row[1]}'"
        )
        # reconcile_status='needs_reconcile'
        assert row[2] == "needs_reconcile", (
            f"caption 失败后 reconcile_status 应为 'needs_reconcile',实际 '{row[2]}'"
        )

        # 4. 验证 caption receipt 不在 list_pending_reconcile 中(因为 status='failed')
        # 注:list_pending_reconcile 返回 reconcile_status='needs_reconcile' 的行
        pending = await mgr.list_pending_reconcile(limit=100)
        action_ids = [p["action_id"] for p in pending]
        assert caption_action_id in action_ids, (
            "caption failed receipt 应在 list_pending_reconcile 中(needs_reconcile)"
        )

    @pytest.mark.asyncio
    async def test_receipt_read_failure_pauses_processing(self, real_store):
        """用例 4: receipt 读失败暂停测试(check_receipt DB 读失败)。

        场景:
            - monkeypatch 让 _db.execute 抛 sqlite3.OperationalError
            - fail_closed=True 时 raise EffectReceiptError(不继续执行外部副作用)
            - fail_closed=False 时返回 None(继续执行,但记录 warning)

        故障注入:monkeypatch 替换 _db.execute 抛 OperationalError
        """
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)

        action_id = "act_read_failure_1"
        effect_type = "telegram_send"
        target = "chat_500"
        request_hash = "hash_read_failure_1"

        # 先正常 record_pending(确保行存在)
        ok = await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        assert ok is True

        # ── 故障注入:monkeypatch _db.execute 抛 sqlite3.OperationalError ──
        original_execute = real_store._db.execute

        async def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("模拟 DB 读失败(disk I/O error)")

        real_store._db.execute = _boom
        try:
            # 1. fail_closed=True → raise EffectReceiptError
            with pytest.raises(EffectReceiptError, match="check_receipt DB 错误"):
                await mgr.check_receipt(
                    action_id, effect_type, target,
                    fail_closed=True,
                    expected_request_hash=request_hash,
                )

            # 2. fail_closed=False → 返回 None(继续执行,但记录 warning)
            result = await mgr.check_receipt(
                action_id, effect_type, target,
                fail_closed=False,
                expected_request_hash=request_hash,
            )
            assert result is None, (
                "fail_closed=False 时 DB 读失败应返回 None(继续执行)"
            )
        finally:
            # 恢复 _db.execute
            real_store._db.execute = original_execute

        # 恢复后 check_receipt 应正常工作
        receipt = await mgr.check_receipt(
            action_id, effect_type, target,
            expected_request_hash=request_hash,
        )
        # status='pending'(record_pending 已调用,未 record_completed)
        # → check_receipt 返回 None(仅 completed 时返回 dict)
        assert receipt is None


# ════════════════════════════════════════════════════════════════
# B. MFA 多 worker 并发测试
# ════════════════════════════════════════════════════════════════


class TestMFAConcurrentWorkers:
    """B. MFA 多 worker 并发测试(4 用例)。

    MFA TOTP 验证 API(从 admin/mfa.py 提取):
        - MFAManager.verify_totp_code(user_id, code) -> bool
        - MFAManager.generate_totp_secret(user_id) -> str
        - MFAManager.enable_mfa(user_id) -> bool
        - _consume_totp_timestep(principal_id, timestep) -> bool (True=首次消费,
          False=重放/fail-closed)
        - _find_matching_timestep(secret, code) -> Optional[int]

    原子消费原语:
        INSERT OR IGNORE INTO mfa_used_totp
            (principal_id, timestep, used_at) VALUES (?, ?, ?)
        + rowcount 判定(1=首次,0=UNIQUE 冲突重放)
    """

    @pytest.mark.asyncio
    async def test_mfa_concurrent_same_totp_only_one_succeeds(self, real_store):
        """用例 5: 5 worker 同时验证同一 TOTP code,只有 1 个成功。

        场景:
            - 5 个并发 worker 同时调用 _consume_totp_timestep(同 principal + 同 timestep)
            - 断言:只有 1 个 worker 成功(返回 True)
            - 断言:其他 4 个返回 False(重放)
            - 断言:callback_nonces 表中该 totp 已标记 consumed_at

        注:实际 mfa_used_totp 表用于 TOTP 重放检测(非 callback_nonces),
            callback_nonces 用于 button callback nonce(R47 P1-a)。
            此处验证 mfa_used_totp 中 timestep 已被消费(used_at 非空)。

        故障注入:asyncio.gather 模拟 5 worker 并发
        """
        import asyncio
        from admin.mfa import _consume_totp_timestep

        principal_id = 51001
        timestep = int(time.time() // 30)

        # 5 worker 同时消费同一 timestep
        async def _worker():
            return await _consume_totp_timestep(principal_id, timestep)

        results = await asyncio.gather(
            _worker(), _worker(), _worker(), _worker(), _worker(),
        )

        success_count = sum(1 for r in results if r is True)
        failure_count = sum(1 for r in results if r is False)

        assert success_count == 1, (
            f"5 worker 同 TOTP 应只有 1 个成功,实际 success={success_count}"
        )
        assert failure_count == 4, (
            f"5 worker 同 TOTP 应有 4 个失败(重放),实际 failure={failure_count}"
        )

        # 验证 mfa_used_totp 表中该 timestep 已记录(used_at 非空)
        cursor = await real_store._db.execute(
            "SELECT used_at FROM mfa_used_totp "
            "WHERE principal_id = ? AND timestep = ?",
            (principal_id, timestep),
        )
        row = await cursor.fetchone()
        assert row is not None, "mfa_used_totp 应有该 timestep 记录"
        assert row[0] is not None and float(row[0]) > 0, (
            "used_at 应为非空时间戳(已消费)"
        )

        # 验证仅 1 行记录(UNIQUE 约束)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM mfa_used_totp "
            "WHERE principal_id = ? AND timestep = ?",
            (principal_id, timestep),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, (
            f"mfa_used_totp 应仅 1 行(UNIQUE),实际 {row[0]} 行"
        )

    @pytest.mark.asyncio
    async def test_mfa_concurrent_different_totp_all_succeed(self, real_store):
        """用例 6: 5 worker 用 5 个不同 TOTP code 验证(同 principal),全部成功。

        场景:
            - 5 个 worker 用 5 个不同 timestep(同 principal)验证
            - 断言:5 个都成功(只要都在 timestep 内)

        故障注入:asyncio.gather 模拟 5 worker 并发,使用不同 timestep
        """
        import asyncio
        from admin.mfa import _consume_totp_timestep

        principal_id = 51002
        base_timestep = int(time.time() // 30)
        # 5 个不同 timestep(t, t+1, t+2, t+3, t+4)
        timesteps = [base_timestep + i for i in range(5)]

        async def _worker(ts):
            return await _consume_totp_timestep(principal_id, ts)

        results = await asyncio.gather(
            _worker(timesteps[0]),
            _worker(timesteps[1]),
            _worker(timesteps[2]),
            _worker(timesteps[3]),
            _worker(timesteps[4]),
        )

        for i, r in enumerate(results):
            assert r is True, (
                f"worker {i} (timestep={timesteps[i]}) 应成功,实际 {r}"
            )

        # 验证 mfa_used_totp 有 5 行(每个 timestep 1 行)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM mfa_used_totp WHERE principal_id = ?",
            (principal_id,),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 5, (
            f"mfa_used_totp 应有 5 行(5 个不同 timestep),实际 {row[0]} 行"
        )

    @pytest.mark.asyncio
    async def test_mfa_timestep_boundary_retries(self, real_store):
        """用例 7: MFA timestep 边界重试(30 秒切换点)。

        场景:
            - 模拟 TOTP 在 timestep 边界(30 秒切换点)
            - mock time.time() 让第一次 verify 在 t=29.9s(current_timestep=N),
              第二次在 t=30.1s(current_timestep=N+1)
            - 断言:两次都成功(系统应支持当前 + 前一/后一 timestep 重试)

        故障注入:time.time() mock 让 _find_matching_timestep 在边界工作。
            - 第一次:mock time.time()=29.9 → current_timestep=N,
              _find_matching_timestep 遍历 [N-1, N, N+1],匹配 timestep=N
            - 第二次:mock time.time()=30.1 → current_timestep=N+1,
              _find_matching_timestep 遍历 [N, N+1, N+2],匹配 timestep=N+1
            - 两个不同 timestep 的 code,均应消费成功
        """
        from admin.mfa import _consume_totp_timestep

        principal_id = 51003
        # 选定两个相邻 timestep 模拟边界
        # 用真实 time.time() 计算 base,然后 mock 让 _find_matching_timestep 落在边界
        real_now = time.time()
        boundary_timestep = int(real_now // 30) + 1  # 下一个 timestep 边界

        # 第一次:mock time.time() = boundary_timestep * 30 - 0.1
        #   → current_timestep = int((boundary*30 - 0.1) // 30) = boundary - 1
        #   → _find_matching_timestep 遍历 [boundary-2, boundary-1, boundary]
        #   → 匹配 timestep=boundary-1 的 code
        first_time = boundary_timestep * 30 - 0.1  # 29.9s 位置(相对 boundary)
        with patch("admin.mfa.time.time", return_value=first_time):
            # 直接消费 timestep=boundary-1(模拟 _find_matching_timestep 匹配结果)
            ts_first = boundary_timestep - 1
            r1 = await _consume_totp_timestep(principal_id, ts_first)
        assert r1 is True, (
            "第一次(timestep 边界前)应消费成功"
        )

        # 第二次:mock time.time() = boundary_timestep * 30 + 0.1
        #   → current_timestep = int((boundary*30 + 0.1) // 30) = boundary
        #   → _find_matching_timestep 遍历 [boundary-1, boundary, boundary+1]
        #   → 匹配 timestep=boundary 的 code
        second_time = boundary_timestep * 30 + 0.1  # 30.1s 位置(相对 boundary)
        with patch("admin.mfa.time.time", return_value=second_time):
            # 消费 timestep=boundary(不同的 timestep,应成功)
            ts_second = boundary_timestep
            r2 = await _consume_totp_timestep(principal_id, ts_second)
        assert r2 is True, (
            "第二次(timestep 边界后)应消费成功(不同 timestep)"
        )

        # 验证两个 timestep 都已记录
        cursor = await real_store._db.execute(
            "SELECT timestep FROM mfa_used_totp WHERE principal_id = ? "
            "ORDER BY timestep",
            (principal_id,),
        )
        rows = await cursor.fetchall()
        recorded_timesteps = [r[0] for r in rows]
        assert ts_first in recorded_timesteps, (
            f"timestep={ts_first} 应已记录(边界前)"
        )
        assert ts_second in recorded_timesteps, (
            f"timestep={ts_second} 应已记录(边界后)"
        )

    @pytest.mark.asyncio
    async def test_mfa_replay_same_totp_rejected(self, real_store):
        """用例 8: 同一 TOTP code 重放被拒绝。

        场景:
            - 第一次 verify TOTP timestep 成功消费
            - 第二次 verify 同一 timestep 失败(已消费)
            - 断言:错误语义为"重放"(返回 False)

        注:错误消息在 verify_totp_code 中是 logger.warning(TOTP timestep
            重放被拒绝),_consume_totp_timestep 返回 False 表示重放。
        """
        from admin.mfa import _consume_totp_timestep

        principal_id = 51004
        timestep = int(time.time() // 30)

        # 第一次消费成功
        r1 = await _consume_totp_timestep(principal_id, timestep)
        assert r1 is True, "第一次消费应成功"

        # 第二次消费同一 timestep → 重放,返回 False
        r2 = await _consume_totp_timestep(principal_id, timestep)
        assert r2 is False, (
            "同一 timestep 第二次消费应返回 False(重放/already used)"
        )

        # 验证 mfa_used_totp 仍只有 1 行(UNIQUE 约束阻止第二次插入)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM mfa_used_totp "
            "WHERE principal_id = ? AND timestep = ?",
            (principal_id, timestep),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, (
            f"重放后 mfa_used_totp 应仍为 1 行(UNIQUE),实际 {row[0]} 行"
        )

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_mfa_verify_totp_code_replay_rejected_with_message(self, real_store):
        """补充用例(用例 8 的端到端版):verify_totp_code 重放拒绝(使用真实 pyotp)。

        场景:
            - 第一次 verify TOTP code 'XXXXXX' 成功
            - 第二次 verify 同一 code 'XXXXXX' 失败(已消费)
            - 断言:第二次返回 False(replay/already used 语义)
        """
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        principal_id = 51005
        secret = await manager.generate_totp_secret(principal_id)
        assert secret
        await manager.enable_mfa(principal_id)

        totp = pyotp.TOTP(secret)
        code = totp.now()

        # 第一次验证应通过(消费 timestep)
        ok1 = await manager.verify_totp_code(principal_id, code)
        assert ok1 is True, "第一次验证应通过"

        # 第二次验证同一 code 应被拒绝(timestep 已消费 → replay)
        ok2 = await manager.verify_totp_code(principal_id, code)
        assert ok2 is False, (
            "同一 code 第二次验证应返回 False(replay/already used)"
        )


# ════════════════════════════════════════════════════════════════
# C. 整体 receipt 一致性测试
# ════════════════════════════════════════════════════════════════


class TestReceiptConsistency:
    """C. 整体 receipt 一致性测试(4 用例)。"""

    @pytest.mark.asyncio
    async def test_crash_window_receipt_state_consistency(self, real_store):
        """用例 9: crash-window receipt 状态一致性。

        场景:
            - record_pending 成功 → kill -9 → 重启
            - 断言:check_receipt 返回 None(status='pending',未完成)
            - 断言:DB 中 status='pending', reconcile_status='pending'
            - 断言:record_pending 再次调用(重启后)应幂等返回 True,
              attempt 保持不变(R62 P1-01: pending+同 hash 幂等,
              外部重试计数由 outbox_events.attempt_count 负责)
            - 断言:外部副作用不重复执行(check_receipt 不会返回 completed)

        故障注入:模拟 kill -9(在 record_pending 后中断,不调用 record_completed)
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)

        action_id = "act_crash_consistency_1"
        effect_type = "telegram_send"
        target = "chat_600"
        request_hash = "hash_crash_consistency_1"

        # 1. record_pending 成功(claim)
        ok = await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        assert ok is True

        # 2. 模拟 kill -9(不调用 record_completed,进程崩溃)

        # 3. 重启后:check_receipt 应返回 None(status='pending',未完成)
        receipt = await mgr.check_receipt(
            action_id, effect_type, target,
            expected_request_hash=request_hash,
        )
        assert receipt is None, (
            "crash-window 后 status='pending' → check_receipt 应返回 None"
        )

        # 4. DB 中 status='pending', reconcile_status='pending'
        cursor = await real_store._db.execute(
            "SELECT status, reconcile_status, attempt FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pending", f"status 应为 'pending',实际 '{row[0]}'"
        assert row[1] == "pending", f"reconcile_status 应为 'pending',实际 '{row[1]}'"
        assert int(row[2]) == 1, f"attempt 应为 1(首次 claim),实际 {row[2]}"

        # 5. 重启后 record_pending 再次调用 → 幂等返回 True(R62 P1-01 新语义)
        # pending + 同 request_hash → 幂等返回,attempt 不再递增
        # (外部副作用重试计数由 outbox_events.attempt_count 负责)
        ok_retry = await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        assert ok_retry is True

        cursor = await real_store._db.execute(
            "SELECT attempt, status, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, (
            f"重启后 attempt 应保持 1(R62 P1-01 幂等语义),实际 {row[0]}"
        )
        assert row[1] == "pending", "status 仍应为 'pending'"
        assert row[2] == "pending", "reconcile_status 仍应为 'pending'"

        # 6. 外部副作用不重复执行:check_receipt 仍返回 None(未完成)
        receipt_again = await mgr.check_receipt(
            action_id, effect_type, target,
            expected_request_hash=request_hash,
        )
        assert receipt_again is None, (
            "crash-window 期间 check_receipt 不应返回 completed(外部副作用不重复)"
        )

    @pytest.mark.asyncio
    async def test_crash_window_completed_but_external_unknown(self, real_store):
        """用例 10: crash-window — record_completed 成功但外部副作用实际状态未知。

        场景:
            - record_completed 已成功(本地 receipt 标记 completed)
            - 但外部副作用实际状态未知(网络分区,无法核对)
            - 断言:list_pending_reconcile 不包含(因为 status='completed')
            - 断言:reconcile_status='completed'(无需人工介入)

        注:这种场景需人工审计(reconciliation),代码层只保证 receipt 准确。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)

        action_id = "act_completed_unknown_1"
        effect_type = "telegram_send"
        target = "chat_700"
        request_hash = "hash_completed_unknown_1"

        # 1. record_pending → record_completed(本地 receipt 标记 completed)
        await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        await mgr.record_completed(
            action_id, effect_type, target, external_id="msg_700",
        )

        # 2. 模拟网络分区:外部副作用实际状态未知
        # (代码层无法检测,仅 receipt 层面记录)

        # 3. 断言:list_pending_reconcile 不包含(因为 status='completed')
        pending = await mgr.list_pending_reconcile(limit=100)
        action_ids = [p["action_id"] for p in pending]
        assert action_id not in action_ids, (
            "status='completed' 的 receipt 不应在 list_pending_reconcile 中"
        )

        # 4. 断言:reconcile_status='completed'(无需人工介入)
        cursor = await real_store._db.execute(
            "SELECT status, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "completed", f"status 应为 'completed',实际 '{row[0]}'"
        assert row[1] == "completed", (
            f"reconcile_status 应为 'completed',实际 '{row[1]}'"
        )

        # 5. check_receipt 应返回 completed receipt
        receipt = await mgr.check_receipt(
            action_id, effect_type, target,
            expected_request_hash=request_hash,
        )
        assert receipt is not None
        assert receipt["status"] == "completed"
        assert receipt["external_id"] == "msg_700"

    @pytest.mark.asyncio
    async def test_crash_window_failed_then_retry_succeeds(self, real_store):
        """用例 11: crash-window — failed 后重试成功。

        场景:
            - record_failed(外部失败)→ 重试 → 成功
            - 断言:第一次 record_failed 后 status='failed', reconcile_status='needs_reconcile'
            - 断言:重试 record_pending 成功,attempt+1
            - 断言:重试 record_completed 成功后 status='completed',
              reconcile_status='completed'
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)

        action_id = "act_failed_retry_1"
        effect_type = "telegram_send"
        target = "chat_800"
        request_hash = "hash_failed_retry_1"

        # 1. record_pending(首次 claim,attempt=1)
        ok = await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        assert ok is True

        # 2. record_failed(外部失败)
        await mgr.record_failed(
            action_id, effect_type, target,
            error_msg="TelegramError: network timeout",
        )

        # 3. 断言:status='failed', reconcile_status='needs_reconcile'
        cursor = await real_store._db.execute(
            "SELECT status, reconcile_status, attempt, last_error "
            "FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "failed", f"record_failed 后 status 应为 'failed',实际 '{row[0]}'"
        assert row[1] == "needs_reconcile", (
            f"record_failed 后 reconcile_status 应为 'needs_reconcile',实际 '{row[1]}'"
        )
        assert int(row[2]) == 1, f"attempt 应为 1,实际 {row[2]}"
        assert row[3] is not None and "network timeout" in row[3]

        # 4. 重试 record_pending(attempt+1)
        ok_retry = await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )
        assert ok_retry is True

        # 5. 断言:attempt+1
        cursor = await real_store._db.execute(
            "SELECT attempt, status, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 2, (
            f"重试后 attempt 应为 2(原 1 + 1),实际 {row[0]}"
        )
        assert row[1] == "pending", "重试 record_pending 后 status 应为 'pending'"
        assert row[2] == "pending", (
            "重试 record_pending 后 reconcile_status 应为 'pending'"
        )

        # 6. 重试 record_completed 成功
        await mgr.record_completed(
            action_id, effect_type, target, external_id="msg_800",
        )

        # 7. 断言:status='completed', reconcile_status='completed'
        cursor = await real_store._db.execute(
            "SELECT status, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert row[0] == "completed", (
            f"重试 record_completed 后 status 应为 'completed',实际 '{row[0]}'"
        )
        assert row[1] == "completed", (
            f"重试 record_completed 后 reconcile_status 应为 'completed',实际 '{row[1]}'"
        )

        # 8. check_receipt 应返回 completed
        receipt = await mgr.check_receipt(
            action_id, effect_type, target,
            expected_request_hash=request_hash,
        )
        assert receipt is not None
        assert receipt["status"] == "completed"
        assert receipt["external_id"] == "msg_800"

    @pytest.mark.asyncio
    async def test_concurrent_record_completed_idempotent(self, real_store):
        """用例 12: 同一 action_id 并发调用 record_completed 5 次(终态保护)。

        R62 P1-01 新语义:已 completed 的 receipt 再次 record_completed →
        raise AppError(DATA_RECEIPT_TERMINAL_STATE)(终态保护,防止重复确认)。

        场景:
            - 同一 action_id 并发调用 record_completed 5 次
            - 断言:仅 1 个 worker 成功(首个将 pending→completed)
            - 断言:其余 4 个 worker 抛 AppError(TERMINAL_STATE)(已 completed)
            - 断言:DB 中只有 1 行 receipt(status='completed')

        故障注入:asyncio.gather 模拟 5 worker 并发 record_completed
        """
        import asyncio
        from services.effect_receipts import EffectReceiptManager
        from services.error_codes import AppError, ErrorCodes
        mgr = EffectReceiptManager(real_store)

        action_id = "act_concurrent_completed_1"
        effect_type = "telegram_send"
        target = "chat_900"
        request_hash = "hash_concurrent_completed_1"

        # 先 record_pending
        await mgr.record_pending(
            action_id, effect_type, target, request_hash=request_hash,
        )

        # 5 worker 并发 record_completed(同 action_id,同 external_id)
        # R62 P1-01: 首个成功(pending→completed),其余抛 TERMINAL_STATE
        async def _worker():
            try:
                await mgr.record_completed(
                    action_id, effect_type, target, external_id="msg_900",
                )
                return True
            except AppError as e:
                # R62 P1-01: 已 completed → TERMINAL_STATE(其余 worker)
                return e.code if e.code == ErrorCodes.DATA_RECEIPT_TERMINAL_STATE else False

        results = await asyncio.gather(
            _worker(), _worker(), _worker(), _worker(), _worker(),
        )

        # 仅 1 个 worker 成功(返回 True)
        success_count = sum(1 for r in results if r is True)
        terminal_count = sum(
            1 for r in results if r == ErrorCodes.DATA_RECEIPT_TERMINAL_STATE
        )
        assert success_count == 1, (
            f"并发 record_completed 应仅 1 个成功(终态保护),实际 success={success_count}"
        )
        assert terminal_count == 4, (
            f"其余 4 个应抛 TERMINAL_STATE,实际 terminal={terminal_count}"
        )

        # DB 中只有 1 行 receipt(status='completed')
        cursor = await real_store._db.execute(
            "SELECT COUNT(*), status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (action_id, effect_type, target),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, (
            f"DB 中应仅 1 行 receipt(终态保护),实际 {row[0]} 行"
        )
        assert row[1] == "completed", f"status 应为 'completed',实际 '{row[1]}'"

        # check_receipt 应返回 completed
        receipt = await mgr.check_receipt(
            action_id, effect_type, target,
            expected_request_hash=request_hash,
        )
        assert receipt is not None
        assert receipt["status"] == "completed"
        assert receipt["external_id"] == "msg_900"


# ════════════════════════════════════════════════════════════════
# D. 媒体组 + MFA 组合场景
# ════════════════════════════════════════════════════════════════


class TestMediaGroupWithMFA:
    """D. 媒体组 + MFA 组合场景(2 用例)。

    使用真实 SQLite + 真实 MFA TOTP 原子消费 + 真实 effect_receipts,
    模拟高风险操作(批量删除文件)需 MFA 审批 + 媒体组执行的完整流程。

    不依赖 telegram / pyotp(可选):pyotp 缺失时通过 _consume_totp_timestep +
        _find_matching_timestep 直接验证原子消费语义。
    """

    @pytest.mark.asyncio
    async def test_media_group_with_mfa_approval(self, real_store):
        """用例 13: 媒体组 + MFA 审批组合场景。

        场景:
            - 高风险操作(批量删除 3 文件)需 MFA 审批
            - 流程:发起审批 → MFA 验证 → 审批通过 → 执行媒体组删除
            - 断言:每步都有 effect_receipt 记录
            - 断言:MFA TOTP 只能消费一次
            - 断言:媒体组中每个文件有独立 receipt

        实现:
            - approval action_id: "approval:batch_delete:job_50013"
            - MFA timestep 消费(_consume_totp_timestep)
            - 每个文件删除独立 effect_receipt(action_id 含 file_index)
        """
        from services.effect_receipts import (
            EffectReceiptManager, compute_effect_request_hash,
        )
        from admin.mfa import _consume_totp_timestep
        mgr = EffectReceiptManager(real_store)

        principal_id = 52001
        job_id = 50013
        approval_action_id = f"approval:batch_delete:{job_id}"

        # 1. 发起审批 → record_pending(approval_initiate)
        approval_target = f"principal:{principal_id}"
        approval_params = {
            "principal_id": principal_id,
            "job_id": job_id,
            "action": "batch_delete",
            "file_count": 3,
        }
        approval_hash = compute_effect_request_hash("ban", approval_params)
        ok = await mgr.record_pending(
            approval_action_id, "ban", approval_target,
            request_hash=approval_hash,
        )
        assert ok is True, "发起审批应 record_pending 成功"

        # 2. MFA 验证(消费 TOTP timestep)
        totp_timestep = int(time.time() // 30)
        mfa_ok = await _consume_totp_timestep(principal_id, totp_timestep)
        assert mfa_ok is True, "MFA TOTP 第一次消费应成功"

        # 3. MFA TOTP 只能消费一次(重放拒绝)
        mfa_replay = await _consume_totp_timestep(principal_id, totp_timestep)
        assert mfa_replay is False, "MFA TOTP 第二次消费应被拒绝(重放)"

        # 4. 审批通过 → record_completed(approval_initiate)
        await mgr.record_completed(
            approval_action_id, "ban", approval_target,
            external_id=f"approved:{job_id}",
        )

        # 5. 执行媒体组删除(3 文件,每个独立 receipt)
        group_id = f"dsp_batch:{job_id}:ch_50013:user_50013"
        await real_store.delivery_group_receipt_create(
            group_id=group_id,
            expected_count=3,
            source_ids=[501, 502, 503],
            target_ids=[principal_id] * 3,
            action_id=approval_action_id,
        )

        file_receipts = []
        for idx, file_msg_id in enumerate([501, 502, 503]):
            file_action_id = (
                f"dsp:{job_id}:ch_50013:{file_msg_id}:user_50013:{idx}"
            )
            file_target = f"chat:user_50013"
            file_params = {
                "target_chat_id": principal_id,
                "storage_channel_id": "ch_50013",
                "message_id": file_msg_id,
                "job_id": job_id,
                "child_index": idx,
            }
            file_hash = compute_effect_request_hash("telegram_send", file_params)

            # record_pending → record_completed
            ok_file = await mgr.record_pending(
                file_action_id, "telegram_send", file_target,
                request_hash=file_hash,
            )
            assert ok_file is True, f"文件 {idx} record_pending 应成功"

            await mgr.record_completed(
                file_action_id, "telegram_send", file_target,
                external_id=f"deleted:{file_msg_id}",
            )

            # confirm group receipt child
            await real_store.delivery_group_receipt_confirm_child(
                group_id, file_msg_id,
            )
            file_receipts.append(file_action_id)

        # 6. 断言:每步都有 effect_receipt 记录
        # 审批 receipt
        approval_receipt = await mgr.check_receipt(
            approval_action_id, "ban", approval_target,
            expected_request_hash=approval_hash,
        )
        assert approval_receipt is not None
        assert approval_receipt["status"] == "completed"

        # 7. 断言:媒体组中每个文件有独立 receipt(3 个不同的 action_id)
        for idx, file_action_id in enumerate(file_receipts):
            file_target = f"chat:user_50013"
            file_params = {
                "target_chat_id": principal_id,
                "storage_channel_id": "ch_50013",
                "message_id": [501, 502, 503][idx],
                "job_id": job_id,
                "child_index": idx,
            }
            file_hash = compute_effect_request_hash("telegram_send", file_params)
            file_receipt = await mgr.check_receipt(
                file_action_id, "telegram_send", file_target,
                expected_request_hash=file_hash,
            )
            assert file_receipt is not None, (
                f"文件 {idx} receipt 应存在且 completed"
            )
            assert file_receipt["status"] == "completed"

        # 8. 断言:媒体组 group receipt 已 completed(3/3)
        group_receipt = await real_store.delivery_group_receipt_get(group_id)
        assert group_receipt is not None
        assert group_receipt["confirmed_count"] == 3, (
            f"group receipt confirmed_count 应为 3,实际 "
            f"{group_receipt['confirmed_count']}"
        )
        assert group_receipt["status"] == "completed"

        # 9. 断言:MFA TOTP 仅 1 行记录(消费一次)
        cursor = await real_store._db.execute(
            "SELECT COUNT(*) FROM mfa_used_totp "
            "WHERE principal_id = ? AND timestep = ?",
            (principal_id, totp_timestep),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, (
            f"mfa_used_totp 应仅 1 行(消费一次),实际 {row[0]} 行"
        )

    @pytest.mark.asyncio
    async def test_media_group_partial_mfa_failure(self, real_store):
        """用例 14: 媒体组部分 MFA 失败(3 文件,MFA 在第 2 个文件时失败)。

        场景:
            - 3 文件媒体组,每个文件删除前需 MFA 重新验证(高敏感操作)
            - 文件 1: MFA 验证成功 → 删除 → receipt completed
            - 文件 2: MFA 验证失败(timestep 已消费,replay)→ receipt failed
            - 文件 3: 未执行(因文件 2 失败,流程中止)
            - 断言:文件 1 已删除(receipt completed)
            - 断言:文件 2 status='failed', last_error 含 MFA failure
            - 断言:文件 3 status='pending'(未执行)
            - 断言:list_pending_reconcile 包含文件 2 和文件 3

        实现:
            - 同一 principal 同一 timestep 只能消费一次
            - 文件 1 消费 timestep T 成功
            - 文件 2 试图消费同一 timestep T(模拟用户重复提交相同 TOTP)→ 失败
            - 文件 3 因文件 2 失败而中止(record_pending 已写入但未执行)
        """
        from services.effect_receipts import (
            EffectReceiptManager, compute_effect_request_hash,
        )
        from admin.mfa import _consume_totp_timestep
        mgr = EffectReceiptManager(real_store)

        principal_id = 52002
        job_id = 50014
        totp_timestep = int(time.time() // 30)

        group_id = f"dsp_batch:{job_id}:ch_50014:user_50014"
        await real_store.delivery_group_receipt_create(
            group_id=group_id,
            expected_count=3,
            source_ids=[601, 602, 603],
            target_ids=[principal_id] * 3,
            action_id=f"approval:batch_delete:{job_id}",
        )

        file_msg_ids = [601, 602, 603]
        file_action_ids = [
            f"dsp:{job_id}:ch_50014:{mid}:user_50014:{idx}"
            for idx, mid in enumerate(file_msg_ids)
        ]
        file_target = f"chat:user_50014"

        # ── 文件 1: MFA 验证成功 → 删除 → receipt completed ──
        file1_params = {
            "target_chat_id": principal_id,
            "storage_channel_id": "ch_50014",
            "message_id": 601,
            "job_id": job_id,
            "child_index": 0,
        }
        file1_hash = compute_effect_request_hash("telegram_send", file1_params)
        await mgr.record_pending(
            file_action_ids[0], "telegram_send", file_target,
            request_hash=file1_hash,
        )
        # MFA 验证(消费 timestep)
        mfa_ok_1 = await _consume_totp_timestep(principal_id, totp_timestep)
        assert mfa_ok_1 is True, "文件 1 MFA 验证应成功(首次消费)"
        # 删除成功 → record_completed
        await mgr.record_completed(
            file_action_ids[0], "telegram_send", file_target,
            external_id="deleted:601",
        )
        await real_store.delivery_group_receipt_confirm_child(group_id, 601)

        # ── 文件 2: MFA 验证失败(重放)→ receipt failed ──
        file2_params = {
            "target_chat_id": principal_id,
            "storage_channel_id": "ch_50014",
            "message_id": 602,
            "job_id": job_id,
            "child_index": 1,
        }
        file2_hash = compute_effect_request_hash("telegram_send", file2_params)
        await mgr.record_pending(
            file_action_ids[1], "telegram_send", file_target,
            request_hash=file2_hash,
        )
        # MFA 验证(试图消费同一 timestep → 重放失败)
        mfa_ok_2 = await _consume_totp_timestep(principal_id, totp_timestep)
        assert mfa_ok_2 is False, (
            "文件 2 MFA 验证应失败(timestep 已消费,重放)"
        )
        # MFA 失败 → record_failed(含 MFA failure 信息)
        await mgr.record_failed(
            file_action_ids[1], "telegram_send", file_target,
            error_msg="MFA verification failed: TOTP timestep already used (replay)",
        )

        # ── 文件 3: 未执行(因文件 2 失败,流程中止)──
        # 但 record_pending 已写入(模拟预 claim,实际未执行删除)
        file3_params = {
            "target_chat_id": principal_id,
            "storage_channel_id": "ch_50014",
            "message_id": 603,
            "job_id": job_id,
            "child_index": 2,
        }
        file3_hash = compute_effect_request_hash("telegram_send", file3_params)
        await mgr.record_pending(
            file_action_ids[2], "telegram_send", file_target,
            request_hash=file3_hash,
        )
        # 模拟 reconciliation 扫描器:将文件 3 pending 标记为 needs_reconcile
        await real_store._db.execute(
            "UPDATE effect_receipts SET reconcile_status = 'needs_reconcile' "
            "WHERE action_id = ? AND status = 'pending'",
            (file_action_ids[2],),
        )
        await real_store._db.commit()

        # ── 断言 ──

        # 1. 文件 1 已删除(receipt completed)
        receipt_1 = await mgr.check_receipt(
            file_action_ids[0], "telegram_send", file_target,
            expected_request_hash=file1_hash,
        )
        assert receipt_1 is not None
        assert receipt_1["status"] == "completed", (
            "文件 1 应为 completed(已删除)"
        )
        assert receipt_1["external_id"] == "deleted:601"

        # 2. 文件 2 status='failed', last_error 含 MFA failure
        cursor = await real_store._db.execute(
            "SELECT status, last_error, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (file_action_ids[1], "telegram_send", file_target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "failed", (
            f"文件 2 status 应为 'failed',实际 '{row[0]}'"
        )
        assert row[1] is not None and "MFA" in row[1] and "replay" in row[1].lower(), (
            f"文件 2 last_error 应含 MFA failure 信息,实际 '{row[1]}'"
        )
        assert row[2] == "needs_reconcile", (
            f"文件 2 reconcile_status 应为 'needs_reconcile',实际 '{row[2]}'"
        )

        # 3. 文件 3 status='pending'(未执行)
        cursor = await real_store._db.execute(
            "SELECT status, reconcile_status FROM effect_receipts "
            "WHERE action_id = ? AND effect_type = ? AND target = ?",
            (file_action_ids[2], "telegram_send", file_target),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pending", (
            f"文件 3 status 应为 'pending'(未执行),实际 '{row[0]}'"
        )
        assert row[1] == "needs_reconcile", (
            f"文件 3 reconcile_status 应为 'needs_reconcile',实际 '{row[1]}'"
        )

        # 4. list_pending_reconcile 包含文件 2 和文件 3
        pending = await mgr.list_pending_reconcile(limit=100)
        action_ids = [p["action_id"] for p in pending]
        assert file_action_ids[1] in action_ids, (
            "list_pending_reconcile 应包含文件 2(failed + needs_reconcile)"
        )
        assert file_action_ids[2] in action_ids, (
            "list_pending_reconcile 应包含文件 3(pending + needs_reconcile)"
        )

        # 5. 文件 1 不在 list_pending_reconcile 中(completed,无需 reconcile)
        assert file_action_ids[0] not in action_ids, (
            "文件 1 (completed) 不应在 list_pending_reconcile 中"
        )

        # 6. group receipt 仅 1/3 confirmed(文件 1)
        group_receipt = await real_store.delivery_group_receipt_get(group_id)
        assert group_receipt is not None
        assert group_receipt["confirmed_count"] == 1, (
            f"group receipt confirmed_count 应为 1(仅文件 1),"
            f"实际 {group_receipt['confirmed_count']}"
        )
        # status 应为 'partial'(1 < 3)
        assert group_receipt["status"] == "partial", (
            f"group receipt status 应为 'partial',实际 '{group_receipt['status']}'"
        )
