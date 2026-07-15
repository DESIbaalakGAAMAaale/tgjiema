"""R53 P1-2: Durable Outbox Hash 异常会形成永久热循环 — 修复回归测试。

被测模块:
- ``database/redis_queue``:replay_durable_outbox() / quarantine_repair()
- ``services/error_codes``:DURABLE_OUTBOX_QUARANTINED

修复背景:
    原 ``replay_durable_outbox`` 中 hash 不匹配时直接 ``continue``,记录仍为
    ``pending``。下一轮 replay 会再次读取、再次校验、再次报错,形成永久日志
    风暴和 CPU/磁盘消耗。

修复方案:
    1. hash 不匹配时 CAS 标记 ``quarantined`` 状态(终止热循环)
    2. 写权威 DLQ(dead letter queue)携带安全摘要(仅 hash 指纹)
    3. quarantined 状态在 SELECT ``WHERE status='pending'`` 中自动过滤
    4. 新增 ``quarantine_repair()`` 函数,经审批后可修复(rehash)或删除
    5. 长批次按行续租 lease(每行抢占前重新计算 lease_until)

测试场景(5 个,覆盖用户要求):
    1. Hash 匹配 → 正常重放
    2. Hash 不匹配 → 标记 quarantined,不再被 pending 查询返回
    3. quarantined 记录可通过 quarantine_repair 函数修复(需审批)
    4. lease 续租机制验证(长批次按行续租)
    5. 批量 hash 不匹配全部进入 quarantined

测试策略:
- 使用 ``conftest.py`` 提供的 ``mock_redis`` / ``mock_settings`` fixtures。
- 每个用例内部用临时目录隔离 durable outbox SQLite 文件。
- 直接修改 SQLite ``data_json`` 模拟 payload 篡改,触发 hash 不匹配。
- 全部用例都不依赖真实 Redis,在本地可独立运行。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from database import redis_queue
from services.error_codes import AppError, ErrorCodes


# ───────────────────────── 测试隔离 fixture ─────────────────────────


@pytest.fixture
def durable_outbox_tmpdir():
    """每个用例使用独立的临时 durable outbox 数据库路径,避免污染生产 data 目录。

    yield 返回临时目录路径,用例结束自动清理。同时确保 durable outbox
    专用连接在用例间被关闭重置,避免上一个用例的连接缓存影响下一个。
    """
    tmpdir = tempfile.mkdtemp(prefix="r53_p1_2_test_")
    original_path = redis_queue._DURABLE_DB_PATH
    redis_queue._DURABLE_DB_PATH = os.path.join(tmpdir, "redis_outbox.db")
    # 重置 durable outbox 专用连接(避免跨用例污染)
    redis_queue._durable_conn = None
    redis_queue._durable_conn_lock = None
    try:
        yield tmpdir
    finally:
        # 同步关闭连接并恢复原路径
        try:
            asyncio.get_event_loop().run_until_complete(
                redis_queue.close_durable_outbox()
            )
        except RuntimeError:
            # 事件循环已关闭或正在运行(异步用例中),忽略
            pass
        except Exception:
            pass
        redis_queue._durable_conn = None
        redis_queue._durable_conn_lock = None
        redis_queue._DURABLE_DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _get_row_id_by_message_id(message_id: str) -> int:
    """根据 message_id 查询 durable_outbox.id(测试辅助函数)。"""
    conn = redis_queue._durable_conn
    cursor = await conn.execute(
        "SELECT id FROM durable_outbox WHERE message_id = ?",
        (message_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None, f"未找到 message_id={message_id} 对应的记录"
    return row[0]


async def _get_status_by_message_id(message_id: str) -> str:
    """根据 message_id 查询 durable_outbox.status(测试辅助函数)。"""
    conn = redis_queue._durable_conn
    cursor = await conn.execute(
        "SELECT status FROM durable_outbox WHERE message_id = ?",
        (message_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None, f"未找到 message_id={message_id} 对应的记录"
    return row[0]


async def _tamper_payload(message_id: str, tampered_data: dict) -> None:
    """直接修改 durable_outbox.data_json 模拟 payload 被篡改(测试辅助函数)。

    修改 data_json 后 request_hash 与新 payload 不匹配,触发 hash 校验失败。
    """
    conn = redis_queue._durable_conn
    await conn.execute(
        "UPDATE durable_outbox SET data_json = ? WHERE message_id = ?",
        (json.dumps(tampered_data, default=str, ensure_ascii=False), message_id),
    )
    await conn.commit()


# ════════════════════════════════════════════════════════════════
# 场景 1: Hash 匹配 → 正常重放
# ════════════════════════════════════════════════════════════════


class TestHashMatchNormalReplay:
    """场景 1: Hash 匹配时正常重放,XADD 被调用一次,状态变为 replayed。"""

    @pytest.mark.asyncio
    async def test_hash_match_replay_success(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """hash 匹配时正常重放,XADD 被调用一次,状态变为 replayed。"""
        # Redis 可达(replayer 需要 XADD 写入 Stream)
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-hash-match-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 1001, "quota": 20},
            message_id=msg_id,
        )
        # 验证 outbox 中有 1 条 pending
        assert await redis_queue.get_durable_outbox_count() == 1

        # 执行重放
        replayed = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed == 1, f"应重放 1 条消息,实际: {replayed}"

        # XADD 被调用一次
        mock_redis.xadd.assert_awaited_once()

        # 验证重放的消息体携带原 message_id
        args, _ = mock_redis.xadd.await_args
        msg = json.loads(args[1]["data"])
        assert msg["message_id"] == msg_id
        assert msg["data"] == {"user_id": 1001, "quota": 20}

        # 验证状态变为 replayed
        status = await _get_status_by_message_id(msg_id)
        assert status == "replayed", f"应为 replayed,实际: {status}"

        # 重放后 outbox 中无 pending 消息
        assert await redis_queue.get_durable_outbox_count() == 0


# ════════════════════════════════════════════════════════════════
# 场景 2: Hash 不匹配 → 标记 quarantined,不再被 pending 查询返回
# ════════════════════════════════════════════════════════════════


class TestHashMismatchQuarantined:
    """场景 2: hash 不匹配 → CAS 标记 quarantined,写 DLQ,终止热循环。"""

    @pytest.mark.asyncio
    async def test_hash_mismatch_quarantined_and_not_returned_by_pending(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """hash 不匹配 → 标记 quarantined,push_dead 被调用,再次 replay 不会读取。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-hash-mismatch-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 2001, "quota": 20},
            message_id=msg_id,
        )

        # 直接修改 data_json 模拟 payload 被篡改(hash 不匹配)
        await _tamper_payload(msg_id, {"user_id": 2001, "quota": 99})

        # Mock push_dead 验证 DLQ 写入
        push_dead_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", push_dead_mock)

        # 执行重放
        replayed = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed == 0, "hash 不匹配时不应重放任何消息"

        # 验证 push_dead 被调用一次(写权威 DLQ)
        push_dead_mock.assert_awaited_once()
        # 验证 DLQ 消息携带安全摘要(不含原始 payload)
        dlq_args, dlq_kwargs = push_dead_mock.await_args
        dlq_msg = dlq_args[0] if dlq_args else dlq_kwargs.get("msg", {})
        assert dlq_msg["message_id"] == msg_id
        assert dlq_msg["method"] == "upsert_user_quota"
        # 安全摘要:仅含 hash 指纹,不应包含原始 data
        assert "expected_hash" in dlq_msg
        assert "actual_hash" in dlq_msg
        # 不应包含完整 payload(避免敏感数据泄露)
        assert "data" not in dlq_msg
        # permanent=True(永久死信,等待人工审核)
        assert dlq_kwargs.get("permanent") is True

        # 验证状态为 quarantined
        status = await _get_status_by_message_id(msg_id)
        assert status == "quarantined", f"应为 quarantined,实际: {status}"

        # 验证 push_dead reason 含 "R53 P1-2" 标识
        reason = dlq_kwargs.get("reason", "")
        assert "R53 P1-2" in reason
        assert "hash mismatch" in reason

        # XADD 不应被调用(hash 不匹配时不重放)
        mock_redis.xadd.assert_not_awaited()

        # 再次重放:quarantined 状态不在 SELECT WHERE status='pending' 中
        push_dead_mock.reset_mock()
        mock_redis.xadd.reset_mock()
        replayed_again = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed_again == 0, "quarantined 状态不应被重放"
        push_dead_mock.assert_not_awaited(), "quarantined 不应再次写 DLQ"
        mock_redis.xadd.assert_not_awaited(), "quarantined 不应触发 XADD"

        # 验证 pending 计数仍为 0(quarantined 不在 pending 中)
        assert await redis_queue.get_durable_outbox_count() == 0


# ════════════════════════════════════════════════════════════════
# 场景 3: quarantined 记录可通过 quarantine_repair 函数修复(需审批)
# ════════════════════════════════════════════════════════════════


class TestQuarantineRepair:
    """场景 3: quarantined 记录通过 quarantine_repair 修复/删除(需审批)。"""

    @pytest.mark.asyncio
    async def test_quarantine_repair_rehash_restores_pending(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """quarantine_repair(rehash) → 重新计算 hash,状态从 quarantined 恢复为 pending。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-rehash-001"
        original_data = {"user_id": 3001, "quota": 20}
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data=original_data,
            message_id=msg_id,
        )
        # 模拟篡改 → quarantined
        await _tamper_payload(msg_id, {"user_id": 3001, "quota": 99})
        monkeypatch.setattr(redis_queue, "push_dead", AsyncMock(return_value=True))
        await redis_queue.replay_durable_outbox(batch_size=100)
        status = await _get_status_by_message_id(msg_id)
        assert status == "quarantined", "前置条件:状态应为 quarantined"

        # 通过 quarantine_repair 修复(用原始正确 payload 重新计算 hash)
        row_id = await _get_row_id_by_message_id(msg_id)
        result = await redis_queue.quarantine_repair(
            row_id=row_id,
            action="rehash",
            approval_action_id="approval-rehash-001",
            new_data=original_data,
        )

        # 验证返回值
        assert result["status"] == "rehashed"
        assert "new_hash" in result
        assert len(result["new_hash"]) == 16  # 短指纹 16 字符

        # 验证状态恢复为 pending
        status_after = await _get_status_by_message_id(msg_id)
        assert status_after == "pending", \
            f"rehash 后应为 pending,实际: {status_after}"

        # 验证可被 replay 重放(终止热循环后,经审批修复可恢复)
        replayed = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed == 1, "修复后应能被重放"
        mock_redis.xadd.assert_awaited_once()

        # 验证最终状态为 replayed
        status_final = await _get_status_by_message_id(msg_id)
        assert status_final == "replayed"

    @pytest.mark.asyncio
    async def test_quarantine_repair_delete_removes_record(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """quarantine_repair(delete) → 物理删除 quarantined 记录。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-delete-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 4001, "quota": 20},
            message_id=msg_id,
        )
        # 触发 quarantined
        await _tamper_payload(msg_id, {"user_id": 4001, "quota": 99})
        monkeypatch.setattr(redis_queue, "push_dead", AsyncMock(return_value=True))
        await redis_queue.replay_durable_outbox(batch_size=100)
        status = await _get_status_by_message_id(msg_id)
        assert status == "quarantined"

        # 通过 quarantine_repair 删除
        row_id = await _get_row_id_by_message_id(msg_id)
        result = await redis_queue.quarantine_repair(
            row_id=row_id,
            action="delete",
            approval_action_id="approval-delete-001",
        )
        assert result["status"] == "deleted"
        assert result["row_id"] == row_id

        # 验证记录已被物理删除
        conn = redis_queue._durable_conn
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM durable_outbox WHERE id = ?",
            (row_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == 0, "delete 后记录应被物理删除"

    @pytest.mark.asyncio
    async def test_quarantine_repair_requires_approval_action_id(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """quarantine_repair 缺少 approval_action_id → AppError(REPAIR_CONSOLE_APPROVAL_REQUIRED)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-no-approval-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 5001, "quota": 20},
            message_id=msg_id,
        )
        await _tamper_payload(msg_id, {"user_id": 5001, "quota": 99})
        monkeypatch.setattr(redis_queue, "push_dead", AsyncMock(return_value=True))
        await redis_queue.replay_durable_outbox(batch_size=100)

        row_id = await _get_row_id_by_message_id(msg_id)

        # 缺少 approval_action_id → AppError
        with pytest.raises(AppError) as exc_info:
            await redis_queue.quarantine_repair(
                row_id=row_id,
                action="rehash",
                approval_action_id="",  # 空 → 拒绝
                new_data={"user_id": 5001, "quota": 20},
            )
        assert exc_info.value.code == ErrorCodes.REPAIR_CONSOLE_APPROVAL_REQUIRED, \
            f"应抛 REPAIR_CONSOLE_APPROVAL_REQUIRED,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_quarantine_repair_rejects_non_quarantined(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """quarantine_repair 对非 quarantined 状态记录 → AppError(APPROVAL_STATE_INVALID)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-non-quarantined-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 6001, "quota": 20},
            message_id=msg_id,
        )
        # 不触发 quarantined,记录保持 pending
        row_id = await _get_row_id_by_message_id(msg_id)
        status = await _get_status_by_message_id(msg_id)
        assert status == "pending"

        # 对 pending 状态调用 quarantine_repair → AppError
        with pytest.raises(AppError) as exc_info:
            await redis_queue.quarantine_repair(
                row_id=row_id,
                action="delete",
                approval_action_id="approval-test-002",
            )
        assert exc_info.value.code == ErrorCodes.APPROVAL_STATE_INVALID, \
            f"应抛 APPROVAL_STATE_INVALID,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_quarantine_repair_rejects_invalid_action(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """quarantine_repair action 非法 → AppError(VALIDATION_FAILED)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-invalid-action-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 7001, "quota": 20},
            message_id=msg_id,
        )
        await _tamper_payload(msg_id, {"user_id": 7001, "quota": 99})
        monkeypatch.setattr(redis_queue, "push_dead", AsyncMock(return_value=True))
        await redis_queue.replay_durable_outbox(batch_size=100)

        row_id = await _get_row_id_by_message_id(msg_id)
        with pytest.raises(AppError) as exc_info:
            await redis_queue.quarantine_repair(
                row_id=row_id,
                action="invalid_action",  # 非法
                approval_action_id="approval-test-003",
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    @pytest.mark.asyncio
    async def test_quarantine_repair_rehash_requires_new_data(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """quarantine_repair(rehash) 缺少 new_data → AppError(VALIDATION_FAILED)。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        msg_id = "uuid-r53-p1-2-rehash-no-data-001"
        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 8001, "quota": 20},
            message_id=msg_id,
        )
        await _tamper_payload(msg_id, {"user_id": 8001, "quota": 99})
        monkeypatch.setattr(redis_queue, "push_dead", AsyncMock(return_value=True))
        await redis_queue.replay_durable_outbox(batch_size=100)

        row_id = await _get_row_id_by_message_id(msg_id)
        with pytest.raises(AppError) as exc_info:
            await redis_queue.quarantine_repair(
                row_id=row_id,
                action="rehash",
                approval_action_id="approval-test-004",
                new_data=None,  # 缺少 → 拒绝
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED


# ════════════════════════════════════════════════════════════════
# 场景 4: lease 续租机制验证(长批次按行续租)
# ════════════════════════════════════════════════════════════════


class TestLeaseRenewalPerRow:
    """场景 4: 长批次下每行抢占 lease 时 lease_until 都基于当前 time.time() 续租。

    修复前: lease_until 在循环外一次性计算(lease_until = now + timeout),
            长批次下后续行的 lease_until 可能已过期,被其他 replayer 重放同一行。
    修复后: 每行抢占 lease 前重新计算 lease_until = time.time() + timeout。
    """

    @pytest.mark.asyncio
    async def test_lease_until_renewed_per_row_in_long_batch(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """长批次下每行 lease_until 都基于当前 time.time() + timeout,
        后续行的 lease_until 严格大于前一行(证明按行续租)。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 写入 3 条 pending 消息(模拟长批次)
        msg_ids = [f"uuid-r53-p1-2-lease-{i:03d}" for i in range(3)]
        for i, mid in enumerate(msg_ids):
            await redis_queue.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 9000 + i, "quota": 20},
                message_id=mid,
            )

        # 捕获每行抢占 lease 时设置的 lease_until
        captured_lease_until: list[float] = []
        conn = redis_queue._durable_conn
        original_execute = conn.execute

        async def capture_execute(sql, *args, **kwargs):
            # 拦截 "UPDATE durable_outbox SET status = 'publishing' ..." 的语句
            if (
                isinstance(sql, str)
                and "publishing" in sql
                and "lease_until = ?" in sql
            ):
                # 参数顺序: (lease_owner, lease_until, row_id)
                captured_lease_until.append(args[0][1])
            return await original_execute(sql, *args, **kwargs)

        conn.execute = capture_execute

        # Mock time.time 让每次调用推进 30 秒(模拟长批次处理时间)
        # 第一个 time.time() 调用是 replay_durable_outbox 顶部的 now = time.time(),
        # 之后每行调用 lease_until = time.time() + _DURABLE_LEASE_TIMEOUT_SECONDS,
        # 还有 replayed_at = time.time() 也调用。
        time_values = [1000.0 + i * 30.0 for i in range(50)]
        time_iter = iter(time_values)
        monkeypatch.setattr(redis_queue.time, "time", lambda: next(time_iter))

        await redis_queue.replay_durable_outbox(batch_size=100)

        # 验证 3 行都执行了抢占 lease
        assert len(captured_lease_until) == 3, \
            f"应捕获 3 行 lease 抢占,实际: {len(captured_lease_until)}"

        # 验证每行 lease_until 严格递增(说明按行续租,而非循环外一次性计算)
        # 若在循环外一次性计算,3 行的 lease_until 应该相同(都是同一个 now + timeout)
        # 修复后每行重新计算,所以后续行的 lease_until 应大于前一行
        assert captured_lease_until[1] > captured_lease_until[0], \
            f"第 2 行 lease_until 应大于第 1 行(按行续租)," \
            f"实际: {captured_lease_until}"
        assert captured_lease_until[2] > captured_lease_until[1], \
            f"第 3 行 lease_until 应大于第 2 行(按行续租)," \
            f"实际: {captured_lease_until}"

        # 验证所有消息都被重放(状态为 replayed)
        for mid in msg_ids:
            status = await _get_status_by_message_id(mid)
            assert status == "replayed", f"{mid} 应为 replayed,实际: {status}"

    @pytest.mark.asyncio
    async def test_lease_until_always_in_future_after_renewal(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """每行抢占 lease 时 lease_until 都大于该行处理时的 time.time()。"""
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 写入 2 条 pending 消息
        for i in range(2):
            await redis_queue.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 9100 + i, "quota": 20},
                message_id=f"uuid-r53-p1-2-lease-future-{i:03d}",
            )

        # 捕获每次 time.time() 调用以及 lease_until 设置
        captured_pairs: list[tuple[float, float]] = []  # (time_at_lease, lease_until)
        conn = redis_queue._durable_conn
        original_execute = conn.execute

        # 用一个共享变量记录最近一次 time.time() 返回值
        last_time_holder = {"value": 1000.0}

        async def capture_execute(sql, *args, **kwargs):
            if (
                isinstance(sql, str)
                and "publishing" in sql
                and "lease_until = ?" in sql
            ):
                # args[0] = (lease_owner, lease_until, row_id)
                lease_until = args[0][1]
                captured_pairs.append((last_time_holder["value"], lease_until))
            return await original_execute(sql, *args, **kwargs)

        conn.execute = capture_execute

        # Mock time.time 让每次调用推进 30 秒,并记录最近一次返回值
        time_counter = [1000.0]

        def mock_time():
            current = time_counter[0]
            time_counter[0] += 30.0
            last_time_holder["value"] = current
            return current

        monkeypatch.setattr(redis_queue.time, "time", mock_time)

        await redis_queue.replay_durable_outbox(batch_size=100)

        # 验证每行 lease_until 都大于当时 time.time()(+lease timeout)
        for idx, (time_at_lease, lease_until) in enumerate(captured_pairs):
            expected_min = time_at_lease + redis_queue._DURABLE_LEASE_TIMEOUT_SECONDS
            assert lease_until >= expected_min, \
                f"第 {idx + 1} 行 lease_until={lease_until} 应 >= " \
                f"time_at_lease({time_at_lease}) + timeout(" \
                f"{redis_queue._DURABLE_LEASE_TIMEOUT_SECONDS}) = {expected_min}"


# ════════════════════════════════════════════════════════════════
# 场景 5: 批量 hash 不匹配全部进入 quarantined
# ════════════════════════════════════════════════════════════════


class TestBatchHashMismatchAllQuarantined:
    """场景 5: 批量 hash 不匹配全部进入 quarantined,无任何消息被重放。"""

    @pytest.mark.asyncio
    async def test_batch_hash_mismatch_all_quarantined(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """多条 hash 不匹配的 pending 消息 → 全部进入 quarantined,
        push_dead 被调用 N 次,无 XADD,无热循环。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 写入 4 条 pending 消息
        msg_ids = [f"uuid-r53-p1-2-batch-{i:03d}" for i in range(4)]
        for i, mid in enumerate(msg_ids):
            await redis_queue.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 10000 + i, "quota": 20},
                message_id=mid,
            )

        # 全部篡改 payload(hash 不匹配)
        for mid in msg_ids:
            await _tamper_payload(mid, {"user_id": 99999, "quota": 99})

        # Mock push_dead 验证 DLQ 写入
        push_dead_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", push_dead_mock)

        # 执行重放
        replayed = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed == 0, "全部 hash 不匹配,不应重放任何消息"

        # push_dead 被调用 4 次(每条 quarantined 都写 DLQ)
        assert push_dead_mock.await_count == 4, \
            f"应调用 push_dead 4 次,实际: {push_dead_mock.await_count}"

        # XADD 不应被调用
        mock_redis.xadd.assert_not_awaited()

        # 验证所有记录状态为 quarantined
        for mid in msg_ids:
            status = await _get_status_by_message_id(mid)
            assert status == "quarantined", \
                f"{mid} 应为 quarantined,实际: {status}"

        # 验证 pending 计数为 0(quarantined 不在 pending 中)
        assert await redis_queue.get_durable_outbox_count() == 0

        # 再次重放:不读 quarantined,无任何调用(终止热循环)
        push_dead_mock.reset_mock()
        mock_redis.xadd.reset_mock()
        replayed_again = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed_again == 0, "quarantined 不应被重放"
        push_dead_mock.assert_not_awaited(), "quarantined 不应再次写 DLQ"
        mock_redis.xadd.assert_not_awaited(), "quarantined 不应触发 XADD"

    @pytest.mark.asyncio
    async def test_batch_mixed_hash_match_and_mismatch(
        self, mock_redis, monkeypatch, durable_outbox_tmpdir,
    ):
        """混合批次:1 条 hash 匹配 + 2 条 hash 不匹配 → 匹配的重放,
        不匹配的进 quarantined,互不干扰。
        """
        monkeypatch.setattr(redis_queue, "get_redis", AsyncMock(return_value=mock_redis))

        # 写入 3 条消息:1 条保持原样(匹配),2 条篡改(不匹配)
        match_msg_id = "uuid-r53-p1-2-mix-match"
        mismatch_msg_ids = [
            "uuid-r53-p1-2-mix-mismatch-1",
            "uuid-r53-p1-2-mix-mismatch-2",
        ]

        await redis_queue.write_durable_outbox(
            op_type="upsert", table="user_quota",
            method_name="upsert_user_quota",
            data={"user_id": 11000, "quota": 20},
            message_id=match_msg_id,
        )
        for i, mid in enumerate(mismatch_msg_ids):
            await redis_queue.write_durable_outbox(
                op_type="upsert", table="user_quota",
                method_name="upsert_user_quota",
                data={"user_id": 12000 + i, "quota": 20},
                message_id=mid,
            )
            # 篡改 payload
            await _tamper_payload(mid, {"user_id": 99999, "quota": 99})

        # Mock push_dead
        push_dead_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(redis_queue, "push_dead", push_dead_mock)

        # 执行重放
        replayed = await redis_queue.replay_durable_outbox(batch_size=100)
        assert replayed == 1, f"应重放 1 条匹配的消息,实际: {replayed}"

        # XADD 被调用一次(仅匹配的消息)
        mock_redis.xadd.assert_awaited_once()
        # push_dead 被调用 2 次(2 条不匹配)
        assert push_dead_mock.await_count == 2, \
            f"应调用 push_dead 2 次,实际: {push_dead_mock.await_count}"

        # 验证状态:匹配的为 replayed,不匹配的为 quarantined
        assert await _get_status_by_message_id(match_msg_id) == "replayed"
        for mid in mismatch_msg_ids:
            assert await _get_status_by_message_id(mid) == "quarantined"

        # 验证 pending 计数为 0(匹配的已 replayed,不匹配的已 quarantined)
        assert await redis_queue.get_durable_outbox_count() == 0


# ════════════════════════════════════════════════════════════════
# 场景 6: DURABLE_OUTBOX_QUARANTINED 错误码注册校验
# ════════════════════════════════════════════════════════════════


class TestDurableOutboxQuarantinedErrorCode:
    """DURABLE_OUTBOX_QUARANTINED 错误码注册校验。"""

    def test_error_code_constant_exists(self):
        """ErrorCodes.DURABLE_OUTBOX_QUARANTINED 常量存在。"""
        assert hasattr(ErrorCodes, "DURABLE_OUTBOX_QUARANTINED")
        assert ErrorCodes.DURABLE_OUTBOX_QUARANTINED == "DURABLE.OUTBOX.QUARANTINED"

    def test_error_code_registered_in_registry(self):
        """DURABLE_OUTBOX_QUARANTINED 已注册到 ErrorRegistry。"""
        from services.error_codes import ErrorRegistry
        assert ErrorRegistry.is_registered(ErrorCodes.DURABLE_OUTBOX_QUARANTINED), \
            "DURABLE_OUTBOX_QUARANTINED 应已注册到 ErrorRegistry"

    def test_error_code_definition_attributes(self):
        """DURABLE_OUTBOX_QUARANTINED 定义属性正确(409, non_retryable, critical)。"""
        from services.error_codes import ErrorRegistry
        definition = ErrorRegistry.get(ErrorCodes.DURABLE_OUTBOX_QUARANTINED)
        assert definition.http_status == 409, \
            f"http_status 应为 409,实际: {definition.http_status}"
        assert definition.retryable is False, \
            f"retryable 应为 False,实际: {definition.retryable}"
        assert definition.severity == "critical", \
            f"severity 应为 critical,实际: {definition.severity}"
        assert definition.message_key == "errors.durable.outbox.quarantined"
        # safe_params 应包含 message_id / method / expected_hash / actual_hash
        for param in ("message_id", "method", "expected_hash", "actual_hash"):
            assert param in definition.safe_params, \
                f"safe_params 应包含 {param},实际: {definition.safe_params}"

    def test_error_code_message_key_in_zh_cn(self):
        """errors.durable.outbox.quarantined 在 zh-CN.json 中存在。"""
        import json
        from pathlib import Path
        locale_path = Path(__file__).resolve().parent.parent / "locales" / "zh-CN.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 扁平化查找
        flat = _flatten_dict_for_test(data)
        assert "errors.durable.outbox.quarantined" in flat, \
            "zh-CN.json 应包含 errors.durable.outbox.quarantined key"
        msg = flat["errors.durable.outbox.quarantined"]
        assert "quarantined" in msg or "隔离" in msg, \
            f"zh-CN 翻译应包含 quarantined 或 隔离,实际: {msg}"

    def test_error_code_message_key_in_en_us(self):
        """errors.durable.outbox.quarantined 在 en-US.json 中存在。"""
        import json
        from pathlib import Path
        locale_path = Path(__file__).resolve().parent.parent / "locales" / "en-US.json"
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        flat = _flatten_dict_for_test(data)
        assert "errors.durable.outbox.quarantined" in flat, \
            "en-US.json 应包含 errors.durable.outbox.quarantined key"
        msg = flat["errors.durable.outbox.quarantined"]
        assert "quarantined" in msg, \
            f"en-US 翻译应包含 quarantined,实际: {msg}"

    def test_app_error_creation_with_quarantined_code(self):
        """AppError(DURABLE_OUTBOX_QUARANTINED) 可正常创建并渲染 i18n message。"""
        from services.error_codes import ErrorRegistry
        err = AppError(
            ErrorCodes.DURABLE_OUTBOX_QUARANTINED,
            params={
                "message_id": "msg-test-001",
                "method": "upsert_user_quota",
                "expected_hash": "abc123",
                "actual_hash": "def456",
            },
            locale="zh-CN",
        )
        assert err.code == ErrorCodes.DURABLE_OUTBOX_QUARANTINED
        assert err.retryable is False
        assert err.severity == "critical"
        # message 应包含 message_id
        assert "msg-test-001" in err.message or "durable.outbox.quarantined" in err.message
        # params 应被 safe_params 过滤
        assert err.params.get("message_id") == "msg-test-001"
        assert err.params.get("method") == "upsert_user_quota"
        assert err.params.get("expected_hash") == "abc123"
        assert err.params.get("actual_hash") == "def456"


def _flatten_dict_for_test(obj, prefix=""):
    """递归扁平化 dict(测试辅助函数)。"""
    result = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_dict_for_test(v, full_key))
        else:
            result[full_key] = v
    return result
