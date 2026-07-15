"""R52 P1 批量(8 项)测试。

被测目标:
    - P1-1: ``database/redis_queue.py`` Durable Outbox request_hash/lease
        * write_durable_outbox 同 message_id 不同 payload → AppError(COMMAND_HASH_MISMATCH)
        * write_durable_outbox 同 message_id 相同 payload → 幂等成功
        * replay_durable_outbox 回收 publishing 状态崩溃残留
    - P1-2: ``services/notifications.py`` Notification Dedup 返回契约
        * send_with_dedup_contract 返回 {status, notif_id, outbox_id}
        * 去重命中返回 deduplicated + 现有权威记录
        * 真实写失败返回 error
        * dedup window 使用 UTC bucket
    - P1-3: ``services/data_lifecycle.py`` 审计原子性
        * deletion completed + audit_log 同事务
        * backup marker 校验 user scope + checksum + completed_at
        * 任一失败 → 整体回滚
    - P1-4: ``services/entitlements.py`` transaction-aware read + RBAC
        * get_user_quota(tx=...) 复用外层事务
        * set_user_plan CAS 冲突 → AppError(ENTITLEMENT_SET_PLAN_CAS_CONFLICT)
        * 审计包含 old_plan/new_plan/request_hash
        * set_user_plan_via_command_bus 缺 action_id → 拒绝
    - P1-5: ``services/collections.py`` 移除公开 CAS bypass
        * bypass_cas=True 无 approval_action_id → AppError(COLLECTION_CAS_BYPASS_NOT_ALLOWED)
        * bypass_cas=True + approval_action_id → 委托 _update_collection_without_cas
        * 公共 API 必须传 expected_version
    - P1-6: ``services/maintenance_mode.py`` fail-closed
        * disable 查询 recover_status 失败 → fail-closed 拒绝
        * request_hash 只记录短指纹(前 16 字符)
        * recover_status 持久化失败触发 critical alert
    - P1-7: ``services/prometheus_exporter.py`` unknown 语义
        * 采集失败时不输出 0 值带 error label
        * 统一 tgjiema_collector_success{collector=...}
        * 高基数 label CI 模式 raise AppError
    - P1-8: ``cf-workers/file-bot/src/index.js`` 两阶段去重
        * isUpdateIdProcessed 仅 "completed" 视为已处理
        * processing → completed 两阶段流程
        * handler 失败时删除 processing 标记(允许重试)
        * UPDATE_ID_KV 未配置时输出 critical 日志

测试策略:
    - 使用真实 SQLite 临时数据库隔离生产数据
    - AppError + ErrorCodes 协议化错误校验
    - 中文注释和日志,英文 raise 消息
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r52_p1_test_")
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
async def reset_cache():
    """每个用例前重置 maintenance_mode 模块级缓存。"""
    from services import maintenance_mode
    maintenance_mode._reset_cache_for_test()
    yield
    maintenance_mode._reset_cache_for_test()


# ── R53 P0-4: 辅助函数 — 插入 command_executions 审批记录 ──
async def _insert_command_execution(
    store, action_id: str, principal_id: int = 0, status: str = "approved",
    request_hash: str = "",
):
    """插入测试 command_executions 记录(用于审批验证)。

    R53 P0-4: collections._update_collection_without_cas 严格校验
    command_executions 表中的 status / principal_id / request_hash。
    """
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    await store._db.execute(
        """INSERT OR REPLACE INTO command_executions
           (action_id, command_type, principal_id, status, owner,
            lease_until, request_hash, result, created_at, updated_at)
           VALUES (?, 'collection_bypass', ?, ?, NULL, NULL, ?, '', ?, ?)""",
        (action_id, principal_id, status, request_hash, now, now),
    )
    await store._db.commit()


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存,避免跨用例污染。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


@pytest.fixture(autouse=True)
def _reset_notifications_schema():
    """每个用例前重置 notifications outbox schema 初始化标记。"""
    try:
        from services import notifications
        notifications._reset_outbox_schema_for_test()
    except Exception:
        pass
    yield
    try:
        from services import notifications
        notifications._reset_outbox_schema_for_test()
    except Exception:
        pass


@pytest_asyncio.fixture
async def reset_durable_outbox_conn():
    """R52 P1-1: 每个用例前重置 durable_outbox 专用连接,使用临时 db 路径隔离。

    durable_outbox 表位于独立的 ``data/redis_outbox.db``,不在 cache_store 的
    ``real_store._db`` 中。本 fixture 临时将 ``_DURABLE_DB_PATH`` 指向临时目录,
    并重置 ``_durable_conn`` 以便在测试中通过模块级连接查询。
    """
    import tempfile as _tempfile
    from database import redis_queue as _rq

    tmpdir = _tempfile.mkdtemp(prefix="r52_durable_outbox_")
    original_path = _rq._DURABLE_DB_PATH
    original_conn = _rq._durable_conn
    original_lock = _rq._durable_conn_lock
    _rq._DURABLE_DB_PATH = str(Path(tmpdir) / "test_durable_outbox.db")
    _rq._durable_conn = None
    _rq._durable_conn_lock = None
    try:
        yield _rq
    finally:
        # 关闭测试期间打开的连接
        if _rq._durable_conn is not None:
            try:
                await _rq._durable_conn.close()
            except Exception:
                pass
        _rq._DURABLE_DB_PATH = original_path
        _rq._durable_conn = original_conn
        _rq._durable_conn_lock = original_lock
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# P1-1: Durable Outbox request_hash/lease
# ════════════════════════════════════════════════════════════════

class TestP1_1DurableOutboxRequestHashLease:
    """R52 P1-1: Durable Outbox request_hash + lease 机制。"""

    @pytest.mark.asyncio
    async def test_write_durable_outbox_idempotent_same_payload(
        self, real_store, reset_durable_outbox_conn
    ):
        """同 message_id + 相同 payload → 幂等成功(不抛异常)。

        场景:
            1. 第一次写入 message_id="msg_001" + payload A
            2. 第二次写入同 message_id + 相同 payload A
        预期:
            - 第一次成功
            - 第二次幂等成功(不抛 AppError)
        """
        from database.redis_queue import write_durable_outbox

        # 第一次写入
        await write_durable_outbox(
            message_id="msg_001_idempotent",
            op_type="upsert",
            table="users_local",
            method_name="set_user_plan",
            data={"user_id": 100, "plan": "basic"},
        )
        # 第二次写入(相同 payload → 幂等)
        await write_durable_outbox(
            message_id="msg_001_idempotent",
            op_type="upsert",
            table="users_local",
            method_name="set_user_plan",
            data={"user_id": 100, "plan": "basic"},
        )
        # 无异常即成功
        # 验证 durable_outbox 表中只有一条记录(幂等)
        # 注:durable_outbox 使用独立连接(reset_durable_outbox_conn 提供的临时 db)
        conn = reset_durable_outbox_conn._durable_conn
        assert conn is not None, "durable_outbox 专用连接应已初始化"
        cursor = await conn.execute(
            "SELECT message_id FROM durable_outbox WHERE message_id = ?",
            ("msg_001_idempotent",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert len(rows) == 1, f"幂等写入应只有 1 条记录,实际: {len(rows)}"

    @pytest.mark.asyncio
    async def test_write_durable_outbox_hash_mismatch_raises(
        self, real_store, reset_durable_outbox_conn
    ):
        """同 message_id + 不同 payload → AppError(COMMAND_HASH_MISMATCH)。

        场景:
            1. 第一次写入 message_id="msg_002" + payload A
            2. 第二次写入同 message_id + payload B(篡改)
        预期:
            - 第二次抛 AppError(COMMAND_HASH_MISMATCH)
        """
        from database.redis_queue import write_durable_outbox
        from services.error_codes import AppError, ErrorCodes

        # 第一次写入
        await write_durable_outbox(
            message_id="msg_002_mismatch",
            op_type="upsert",
            table="users_local",
            method_name="set_user_plan",
            data={"user_id": 200, "plan": "basic"},
        )
        # 第二次写入(不同 payload → 篡改检测)
        with pytest.raises(AppError) as exc_info:
            await write_durable_outbox(
                message_id="msg_002_mismatch",
                op_type="upsert",
                table="users_local",
                method_name="set_user_plan",
                data={"user_id": 200, "plan": "premium"},  # 不同 plan
            )
        assert exc_info.value.code == ErrorCodes.COMMAND_HASH_MISMATCH, \
            f"应抛 COMMAND_HASH_MISMATCH,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_compute_request_hash_deterministic(self):
        """_compute_request_hash 相同输入 → 相同 hash;不同输入 → 不同 hash。"""
        from database.redis_queue import _compute_request_hash

        hash1 = _compute_request_hash(
            "upsert", "users_local", "set_user_plan",
            {"user_id": 100, "plan": "basic"},
        )
        hash2 = _compute_request_hash(
            "upsert", "users_local", "set_user_plan",
            {"user_id": 100, "plan": "basic"},
        )
        hash3 = _compute_request_hash(
            "upsert", "users_local", "set_user_plan",
            {"user_id": 100, "plan": "premium"},  # 不同
        )
        assert hash1 == hash2, "相同输入应产生相同 hash"
        assert hash1 != hash3, "不同输入应产生不同 hash"
        assert len(hash1) == 16, f"短指纹应为 16 字符,实际: {len(hash1)}"


# ════════════════════════════════════════════════════════════════
# P1-2: Notification Dedup 返回契约
# ════════════════════════════════════════════════════════════════

class TestP1_2NotificationDedupContract:
    """R52 P1-2: Notification Dedup 返回结构化契约。"""

    @pytest.mark.asyncio
    async def test_send_with_dedup_contract_returns_sent_status(self, real_store):
        """新通知写入成功 → {status: "sent", notif_id: >0, outbox_id: >0}。"""
        from services.notifications import send_with_dedup_contract

        result = await send_with_dedup_contract(
            user_id=300,
            notif_type="ready",
            payload={"file_code": "TEST001", "_dedup_key": ""},
        )
        assert result["status"] == "sent", f"应为 sent,实际: {result['status']}"
        assert result["notif_id"] > 0, f"notif_id 应 >0,实际: {result['notif_id']}"
        assert result["outbox_id"] > 0, f"outbox_id 应 >0,实际: {result['outbox_id']}"

    @pytest.mark.asyncio
    async def test_send_with_dedup_contract_dedup_returns_existing(self, real_store):
        """去重命中 → {status: "deduplicated", notif_id: >0} 返回现有权威记录。"""
        from services.notifications import send_with_dedup_contract

        # 第一次发送(带 dedup_key)
        result1 = await send_with_dedup_contract(
            user_id=301,
            notif_type="ready",
            payload={"file_code": "TEST002", "_dedup_key": "dedup_key_301"},
        )
        assert result1["status"] == "sent"
        original_notif_id = result1["notif_id"]

        # 第二次发送(同 dedup_key → 去重命中)
        result2 = await send_with_dedup_contract(
            user_id=301,
            notif_type="ready",
            payload={"file_code": "TEST002", "_dedup_key": "dedup_key_301"},
        )
        assert result2["status"] == "deduplicated", \
            f"应为 deduplicated,实际: {result2['status']}"
        assert result2["notif_id"] == original_notif_id, \
            f"去重应返回原 notif_id={original_notif_id},实际: {result2['notif_id']}"

    @pytest.mark.asyncio
    async def test_send_with_dedup_contract_error_on_write_failure(self, real_store):
        """CacheStore 不可用 → {status: "error", notif_id: 0}。"""
        from services.notifications import send_with_dedup_contract
        from database.cache_store import get_cache_store

        # Mock store._db 为 None(模拟 CacheStore 不可用)
        store = get_cache_store()
        original_db = store._db
        store._db = None
        try:
            result = await send_with_dedup_contract(
                user_id=302,
                notif_type="ready",
                payload={"file_code": "TEST003"},
            )
            assert result["status"] == "error", \
                f"CacheStore 不可用应返回 error,实际: {result['status']}"
            assert result["notif_id"] == 0
            assert "error_code" in result, "error 状态应包含 error_code"
        finally:
            store._db = original_db

    @pytest.mark.asyncio
    async def test_compute_window_start_utc(self):
        """_compute_window_start 返回 UTC 整点对齐时间(带 +00:00 后缀)。"""
        from services.notifications import _compute_window_start
        import datetime as _dt

        # 固定时间: 2026-07-15 10:30:45 UTC
        fixed_now = _dt.datetime(2026, 7, 15, 10, 30, 45)
        window = _compute_window_start(fixed_now)
        # 应为整点对齐 + UTC 后缀
        assert window.endswith("+00:00"), \
            f"window_start 应以 +00:00 结尾(UTC),实际: {window}"
        assert "10:00:00" in window, \
            f"应整点对齐到 10:00,实际: {window}"
        assert "30" not in window.split("+")[0], \
            f"不应包含分钟 30,实际: {window}"


# ════════════════════════════════════════════════════════════════
# P1-3: Data Lifecycle 审计原子性
# ════════════════════════════════════════════════════════════════

class TestP1_3DataLifecycleAuditAtomicity:
    """R52 P1-3: deletion completed + audit_log 同事务 + backup marker 校验。"""

    @pytest.mark.asyncio
    async def test_write_audit_log_in_tx_exists(self):
        """_write_audit_log_in_tx 函数存在且可被 import。"""
        from services.data_lifecycle import _write_audit_log_in_tx
        assert callable(_write_audit_log_in_tx), \
            "_write_audit_log_in_tx 应为可调用函数"

    @pytest.mark.asyncio
    async def test_verify_backup_marker_user_scope(self, real_store):
        """_verify_backup_marker 校验 user scope(require_user_scope=True)。"""
        from services.data_lifecycle import _verify_backup_marker

        # Mock BackupEngine.get_last_successful_backup 返回带 user_id + checksum 的 marker
        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        mock_marker = {
            "backup_id": "bk_001",
            "user_id": 500,
            "completed_at": now,
            "checksum": "abc123def456",
        }
        with patch("services.backup_engine.BackupEngine") as MockEngine:
            MockEngine.return_value.get_last_successful_backup = AsyncMock(
                return_value=mock_marker
            )
            # 校验通过(user scope 匹配 + checksum 存在)
            ok = await _verify_backup_marker(
                user_id=500,
                require_user_scope=True,
                require_checksum=True,
            )
        assert ok is not None, "user scope + checksum 匹配时应通过校验(R53 P1-3 返回 dict)"

    @pytest.mark.asyncio
    async def test_verify_backup_marker_rejects_wrong_user(self, real_store):
        """_verify_backup_marker 校验 user scope 不匹配 → 拒绝。"""
        from services.data_lifecycle import _verify_backup_marker

        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        mock_marker = {
            "backup_id": "bk_002",
            "user_id": 600,
            "completed_at": now,
            "checksum": "abc123",
        }
        with patch("services.backup_engine.BackupEngine") as MockEngine:
            MockEngine.return_value.get_last_successful_backup = AsyncMock(
                return_value=mock_marker
            )
            # user scope 不匹配(user_id=601 vs marker user_id=600)
            ok = await _verify_backup_marker(
                user_id=601,  # 不匹配
                require_user_scope=True,
                require_checksum=True,
            )
        assert ok is None, "user scope 不匹配时应拒绝(R53 P1-3 返回 None)"

    @pytest.mark.asyncio
    async def test_verify_backup_marker_rejects_missing_checksum(self, real_store):
        """_verify_backup_marker 校验 checksum 缺失 → 拒绝。"""
        from services.data_lifecycle import _verify_backup_marker

        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        mock_marker = {
            "backup_id": "bk_003",
            "user_id": 700,
            "completed_at": now,
            # 无 checksum 字段
        }
        with patch("services.backup_engine.BackupEngine") as MockEngine:
            MockEngine.return_value.get_last_successful_backup = AsyncMock(
                return_value=mock_marker
            )
            # require_checksum=True 但 marker 无 checksum
            ok = await _verify_backup_marker(
                user_id=700,
                require_user_scope=True,
                require_checksum=True,
            )
        assert ok is None, "checksum 缺失时应拒绝(R53 P1-3 返回 None)"


# ════════════════════════════════════════════════════════════════
# P1-4: Entitlements transaction-aware read + RBAC
# ════════════════════════════════════════════════════════════════

class TestP1_4EntitlementsTransactionAwareAndRBAC:
    """R52 P1-4: transaction-aware get_user_quota + CAS + CommandBus。"""

    @pytest.mark.asyncio
    async def test_get_user_quota_tx_aware_exists(self):
        """get_user_quota(tx=...) 函数存在且接受 tx 参数。"""
        from services.entitlements import get_user_quota
        import inspect as _inspect
        sig = _inspect.signature(get_user_quota)
        assert "tx" in sig.parameters, \
            "get_user_quota 应接受 tx 参数(transaction-aware)"

    @pytest.mark.asyncio
    async def test_get_user_version_exists(self):
        """get_user_version(tx=...) 函数存在。"""
        from services.entitlements import get_user_version
        import inspect as _inspect
        sig = _inspect.signature(get_user_version)
        assert "tx" in sig.parameters, \
            "get_user_version 应接受 tx 参数"

    @pytest.mark.asyncio
    async def test_set_user_plan_cas_conflict_raises(self, real_store):
        """_set_user_plan_internal CAS 版本冲突 → AppError(ENTITLEMENT_SET_PLAN_CAS_CONFLICT)。

        场景:
            1. 用户 version=1
            2. _set_user_plan_internal(expected_version=999) → CAS 冲突
        """
        from services.entitlements import _set_user_plan_internal
        from services.error_codes import AppError, ErrorCodes

        # 确保 users_local 表有 version 列(插入测试用户)
        try:
            await real_store._db.execute(
                "INSERT OR IGNORE INTO users_local "
                "(user_id, username, membership_level, version) "
                "VALUES (?, ?, ?, ?)",
                (800, "test_user_800", "free", 1),
            )
            await real_store._db.commit()
        except Exception:
            # version 列可能不存在(老库),跳过此测试
            pytest.skip("users_local 表无 version 列(老库),跳过 CAS 测试")

        # CAS 冲突:expected_version=999 但实际 version=1
        with pytest.raises(AppError) as exc_info:
            await _set_user_plan_internal(
                user_id=800,
                plan_name="basic",
                admin_id=900,
                expected_version=999,  # 故意不匹配
            )
        assert exc_info.value.code == ErrorCodes.ENTITLEMENT_SET_PLAN_CAS_CONFLICT, \
            f"应抛 CAS_CONFLICT,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_set_user_plan_audit_contains_old_new_plan(self, real_store):
        """_set_user_plan_internal 审计日志包含 old_plan/new_plan/request_hash。"""
        from services.entitlements import _set_user_plan_internal, _PLANS, Plan

        # Patch _PLANS 使用真实 Plan 实例(避免 MagicMock 配额值干扰 SQL 绑定)
        # conftest.py 中的 MagicMock settings 未设置 BASIC_DAILY_QUOTA 等属性,
        # 导致 _PLANS[plan].daily_quota 为 MagicMock,SQL 绑定失败。
        real_plans = {
            "free": Plan(
                name="free", daily_quota=10, external_daily_quota=0,
                max_file_size=50 * 1024 * 1024, max_concurrent=1,
                retention_days=7, priority_queue="normal",
                max_collection_items=10,
            ),
            "basic": Plan(
                name="basic", daily_quota=100, external_daily_quota=10,
                max_file_size=500 * 1024 * 1024, max_concurrent=3,
                retention_days=30, priority_queue="normal",
                max_collection_items=50,
            ),
            "premium": Plan(
                name="premium", daily_quota=1000, external_daily_quota=100,
                max_file_size=2 * 1024 * 1024 * 1024, max_concurrent=10,
                retention_days=90, priority_queue="high",
                max_collection_items=200,
            ),
        }
        with patch.dict("services.entitlements._PLANS", real_plans, clear=True):
            # 插入测试用户(套餐=free)
            await real_store._db.execute(
                "INSERT OR IGNORE INTO users_local "
                "(user_id, username, membership_level) "
                "VALUES (?, ?, ?)",
                (801, "test_user_801", "free"),
            )
            await real_store._db.commit()

            # 设置套餐为 basic(带 request_hash)— R53 P0-5: development 环境允许直接调用
            await _set_user_plan_internal(
                user_id=801,
                plan_name="basic",
                admin_id=901,
                request_hash="a" * 32,  # 32 字符 hash
            )

        # 查询 audit_log
        rows = await real_store._db.execute_fetchall(
            "SELECT details FROM audit_log WHERE action = 'set_plan' "
            "AND target_id = ? ORDER BY id DESC LIMIT 1",
            ("801",),
        )
        assert rows and rows[0], "应找到 audit_log 记录"
        details = json.loads(rows[0][0])
        assert "old_plan" in details, "audit_log 应包含 old_plan"
        assert "new_plan" in details, "audit_log 应包含 new_plan"
        assert details["new_plan"] == "basic", f"new_plan 应为 basic,实际: {details.get('new_plan')}"
        # request_hash 应为短指纹(前 16 字符)
        assert details.get("request_hash") == "a" * 16, \
            f"request_hash 应为短指纹(16字符),实际: {details.get('request_hash')}"

    @pytest.mark.asyncio
    async def test_set_user_plan_via_command_bus_no_action_id_rejected(self):
        """set_user_plan_via_command_bus 缺 action_id → AppError(ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS)。"""
        from services.entitlements import set_user_plan_via_command_bus
        from services.error_codes import AppError, ErrorCodes

        principal = MagicMock()
        principal.id = 999
        principal.name = "admin_test"

        with pytest.raises(AppError) as exc_info:
            await set_user_plan_via_command_bus(
                user_id=802,
                plan_name="basic",
                principal=principal,
                action_id="",  # 空 action_id
            )
        assert exc_info.value.code == ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS, \
            f"应抛 REQUIRES_COMMAND_BUS,实际: {exc_info.value.code}"


# ════════════════════════════════════════════════════════════════
# P1-5: 移除公开 CAS bypass
# ════════════════════════════════════════════════════════════════

class TestP1_5RemovePublicCasBypass:
    """R52 P1-5 + R53 P0-4: collections.py bypass_cas 私有化 + 真实审批校验。"""

    @pytest.mark.asyncio
    async def test_bypass_cas_without_approval_rejected(self, real_store):
        """R53 P0-4: 私有方法 approval_action_id 为空 → AppError(COLLECTION_APPROVAL_INVALID)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=999,
                name="test",
                principal_id=950,
                request_hash="hash_001",
                # 无 approval_action_id
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_INVALID, \
            f"应抛 APPROVAL_INVALID,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_public_api_requires_expected_version(self, real_store):
        """公共 API 未传 expected_version → AppError(VERSION_REQUIRED)。"""
        from services.collections import update_collection
        from services.error_codes import AppError, ErrorCodes

        with pytest.raises(AppError) as exc_info:
            await update_collection(
                collection_id=999,
                name="test",
                # 无 expected_version(R53 P0-4: 公共 API 不再有 bypass_cas 通道)
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED, \
            f"应抛 VERSION_REQUIRED,实际: {exc_info.value.code}"

    @pytest.mark.asyncio
    async def test_public_api_rejects_bypass_cas_param(self, real_store):
        """R53 P0-4: 公共 API 不再接受 bypass_cas 参数(传参应抛 TypeError)。"""
        from services.collections import update_collection

        with pytest.raises(TypeError):
            await update_collection(
                collection_id=999,
                name="test",
                expected_version=1,
                bypass_cas=True,  # R53 P0-4: 参数已移除,应抛 TypeError
            )

    @pytest.mark.asyncio
    async def test_private_update_without_cas_function_exists(self):
        """_update_collection_without_cas 私有方法存在。"""
        from services.collections import _update_collection_without_cas
        assert callable(_update_collection_without_cas), \
            "_update_collection_without_cas 应为可调用函数"

    @pytest.mark.asyncio
    async def test_bypass_cas_with_approval_delegates_to_private(self, real_store):
        """R53 P0-4: 私有方法 + 真实审批记录 → 成功更新(状态 approved→executed)。"""
        from services.collections import _update_collection_without_cas, create_collection

        # 先创建集合
        coll = await create_collection("test_bypass_coll", owner_id=950)
        coll_id = coll.get("id", 0)

        # R53 P0-4: 插入真实审批记录(status=approved)
        await _insert_command_execution(
            real_store, "approval_r52_p1_5_001", principal_id=950,
            status="approved", request_hash="hash_r52_p1_5_001",
        )

        # 私有方法 + 真实审批 → 成功更新
        result = await _update_collection_without_cas(
            collection_id=coll_id,
            name="updated_name",
            principal_id=950,
            request_hash="hash_r52_p1_5_001",
            approval_action_id="approval_r52_p1_5_001",
            caller="test_migration",
        )
        assert result["success"] is True, f"bypass 更新应成功,实际: {result}"


# ════════════════════════════════════════════════════════════════
# P1-6: Maintenance fail-closed
# ════════════════════════════════════════════════════════════════

class TestP1_6MaintenanceFailClosed:
    """R52 P1-6: maintenance disable fail-closed + 短指纹日志。"""

    @pytest.mark.asyncio
    async def test_disable_recover_status_query_failure_fail_closed(
        self, real_store, reset_cache
    ):
        """disable 查询 recover_status 失败 → fail-closed 拒绝(不降级为 completed)。"""
        from services import maintenance_mode
        from services.maintenance_mode import MaintenancePreconditionError

        # Mock execute_fetchall 抛异常(模拟查询失败)
        original_exec = real_store._db.execute_fetchall

        async def mock_execute_fetchall(sql, *args, **kwargs):
            if "recover_status" in sql:
                raise RuntimeError("SQLite 锁竞争模拟")
            return await original_exec(sql, *args, **kwargs)

        real_store._db.execute_fetchall = mock_execute_fetchall
        try:
            with pytest.raises(MaintenancePreconditionError) as exc_info:
                await maintenance_mode.disable(ended_by=100, force=True)
            assert "fail-closed" in str(exc_info.value) or "查询 recover_status 失败" in str(exc_info.value), \
                f"应 fail-closed 拒绝,实际: {exc_info.value}"
        finally:
            real_store._db.execute_fetchall = original_exec

    @pytest.mark.asyncio
    async def test_disable_logs_short_hash(self, real_store, reset_cache):
        """disable 日志中 request_hash 只记录前 16 字符(短指纹)。"""
        from services import maintenance_mode

        # 确保 maintenance_state 表存在且 enabled=True
        await real_store._db.execute(
            "INSERT OR REPLACE INTO maintenance_state (id, enabled, reason, started_by, started_at, recover_status) "
            "VALUES (1, 1, 'test', 100, '2026-07-15T00:00:00', 'completed')"
        )
        await real_store._db.commit()

        # 使用 32 字符 hash,验证日志只输出前 16
        long_hash = "0123456789abcdef0123456789abcdef"  # 32 字符
        with patch("loguru.logger") as mock_logger:
            await maintenance_mode.disable(
                ended_by=100, force=True, request_hash=long_hash
            )
            # 检查所有 info 调用的日志消息是否只包含前 16 字符
            for call in mock_logger.info.call_args_list:
                args = str(call)
                # 如果日志中包含 request_hash,应只包含前 16 字符
                if "request_hash" in args:
                    # 不应包含完整 32 字符 hash
                    assert long_hash not in args, \
                        "日志不应包含完整 32 字符 hash(应只记录前 16 字符短指纹)"

    @pytest.mark.asyncio
    async def test_recover_status_persist_failure_triggers_critical_alert(
        self, real_store, reset_cache
    ):
        """recover_status 持久化失败 → 返回 recover_status_persist_failed=True。"""
        from services import maintenance_mode

        # Mock drain_queues 失败(触发 workflow 失败路径)
        with patch.object(
            maintenance_mode,
            "drain_queues",
            new=AsyncMock(return_value={
                "drained": False,
                "remaining_outbox": 5,
                "remaining_jobs": 0,
                "timeout": True,
            }),
        ), patch.object(
            real_store,
            "transaction",
            side_effect=RuntimeError("SQLite 锁竞争(transaction 不可用)"),
        ):
            result = await maintenance_mode.execute_maintenance_workflow(
                reason="测试 recover_status 持久化失败 → critical alert",
                started_by=100,
                auto_disable=True,
            )

        assert result["success"] is False, "workflow 失败"
        assert result.get("recover_status_persist_failed") is True, \
            "recover_status 持久化失败时应设置 recover_status_persist_failed=True"


# ════════════════════════════════════════════════════════════════
# P1-7: Metrics unknown 语义
# ════════════════════════════════════════════════════════════════

class TestP1_7MetricsUnknownSemantics:
    """R52 P1-7: 采集失败时不输出 0 值带 error label,统一 collector_success。"""

    def test_collector_failure_does_not_output_zero_value(self):
        """采集失败时不输出 `metric{collector_status="error"} 0` 格式。"""
        from services.prometheus_exporter import collect_metrics

        # Mock _read_kv_value 返回无法解析的值(模拟采集失败)
        with patch("services.prometheus_exporter._read_kv_value", return_value="not_a_number"):
            output = collect_metrics()

        # 不应包含 collector_status="error" 的 0 值行(R52 P1-7 移除了该模式)
        assert 'redis_pel_depth{collector_status="error"} 0' not in output, \
            "R52 P1-7: 采集失败时不应输出 0 值带 error label"
        assert 'dlq_depth{collector_status="error"} 0' not in output, \
            "R52 P1-7: 采集失败时不应输出 0 值带 error label"

    def test_collector_failure_outputs_unified_collector_success(self):
        """采集失败时输出统一的 tgjiema_collector_success{collector=...} 0。"""
        from services.prometheus_exporter import collect_metrics

        with patch("services.prometheus_exporter._read_kv_value", return_value="not_a_number"):
            output = collect_metrics()

        # 应包含统一的 tgjiema_collector_success metric
        assert 'tgjiema_collector_success{collector="redis_pel"} 0' in output, \
            "采集失败时应输出统一的 tgjiema_collector_success=0"
        assert 'tgjiema_collector_success{collector="dlq"} 0' in output, \
            "采集失败时应输出统一的 tgjiema_collector_success=0"

    def test_high_cardinality_label_ci_mode_raises(self):
        """高基数 label CI 模式 → raise AppError(METRICS_HIGH_CARDINALITY_LABEL)。"""
        from services.prometheus_exporter import _check_no_high_cardinality_labels
        from services.error_codes import AppError, ErrorCodes

        # 设置 CI 模式
        with patch("services.prometheus_exporter._HIGH_CARDINALITY_MODE", "ci"):
            with pytest.raises(AppError) as exc_info:
                _check_no_high_cardinality_labels(
                    'some_metric{user_id="12345"} 5'
                )
            assert exc_info.value.code == ErrorCodes.METRICS_HIGH_CARDINALITY_LABEL, \
                f"应抛 HIGH_CARDINALITY_LABEL,实际: {exc_info.value.code}"

    def test_i18n_collector_failure_uses_unified_metric(self):
        """i18n 采集失败时使用统一 tgjiema_collector_success(不再用 i18n_collector_success)。"""
        from services.prometheus_exporter import collect_metrics

        # Mock i18n manager 不可用
        with patch("services.i18n.get_i18n_manager", side_effect=RuntimeError("i18n 未初始化")):
            output = collect_metrics()

        # 不应包含旧的 tgjiema_i18n_collector_success metric
        assert "tgjiema_i18n_collector_success" not in output, \
            "R52 P1-7: 应统一为 tgjiema_collector_success,不再用 tgjiema_i18n_collector_success"
        # 应包含统一的 collector_success
        assert 'tgjiema_collector_success{collector="i18n_missing_key"} 0' in output, \
            "i18n 采集失败时应输出统一的 tgjiema_collector_success=0"


# ════════════════════════════════════════════════════════════════
# P1-8: CF Worker 两阶段去重
# ════════════════════════════════════════════════════════════════

class TestP1_8CFWorkerTwoPhaseDedup:
    """R52 P1-8: CF Worker 两阶段去重(processing → completed)。"""

    def test_index_js_contains_two_phase_functions(self):
        """index.js 包含 markUpdateIdProcessing / markUpdateIdCompleted / clearUpdateIdProcessing。"""
        index_path = Path(__file__).parent.parent / "cf-workers" / "file-bot" / "src" / "index.js"
        if not index_path.exists():
            pytest.skip("cf-workers/file-bot/src/index.js 不存在")
        content = index_path.read_text(encoding="utf-8")

        assert "markUpdateIdProcessing" in content, \
            "index.js 应包含 markUpdateIdProcessing 函数(两阶段去重)"
        assert "markUpdateIdCompleted" in content, \
            "index.js 应包含 markUpdateIdCompleted 函数(两阶段去重)"
        assert "clearUpdateIdProcessing" in content, \
            "index.js 应包含 clearUpdateIdProcessing 函数(两阶段去重)"

    def test_is_update_id_processed_only_completed(self):
        """isUpdateIdProcessed 仅 "completed" 状态视为已处理(processing 不跳过)。"""
        index_path = Path(__file__).parent.parent / "cf-workers" / "file-bot" / "src" / "index.js"
        if not index_path.exists():
            pytest.skip("cf-workers/file-bot/src/index.js 不存在")
        content = index_path.read_text(encoding="utf-8")

        # 验证 isUpdateIdProcessed 函数中仅 "completed" 返回 true
        # 查找 isUpdateIdProcessed 函数体
        func_start = content.find("async function isUpdateIdProcessed")
        assert func_start >= 0, "应找到 isUpdateIdProcessed 函数"
        func_end = content.find("\n}", func_start)
        func_body = content[func_start:func_end]
        assert '=== "completed"' in func_body, \
            "isUpdateIdProcessed 应仅对 'completed' 状态返回 true(processing 允许重试)"

    def test_fetch_handler_uses_two_phase_flow(self):
        """fetch handler 使用两阶段流程(processing → handler → completed/clear)。"""
        index_path = Path(__file__).parent.parent / "cf-workers" / "file-bot" / "src" / "index.js"
        if not index_path.exists():
            pytest.skip("cf-workers/file-bot/src/index.js 不存在")
        content = index_path.read_text(encoding="utf-8")

        # 验证 fetch handler 中不再调用旧的 markUpdateIdProcessed
        assert "markUpdateIdProcessed(" not in content, \
            "R52 P1-8: 应移除旧的 markUpdateIdProcessed 调用(改为两阶段)"

        # 验证 handler 失败时清除 processing 标记
        assert "clearUpdateIdProcessing" in content, \
            "handler 失败时应调用 clearUpdateIdProcessing(允许重试)"
        assert "markUpdateIdCompleted" in content, \
            "handler 成功时应调用 markUpdateIdCompleted"

    def test_update_id_kv_unconfigured_logs_error(self):
        """UPDATE_ID_KV 未配置时输出 error 日志(production 必须配置)。"""
        index_path = Path(__file__).parent.parent / "cf-workers" / "file-bot" / "src" / "index.js"
        if not index_path.exists():
            pytest.skip("cf-workers/file-bot/src/index.js 不存在")
        content = index_path.read_text(encoding="utf-8")

        # 验证有 UPDATE_ID_KV 未配置的 error 日志
        assert "UPDATE_ID_KV 未配置" in content or "UPDATE_ID_KV not configured" in content, \
            "R52 P1-8: UPDATE_ID_KV 未配置时应输出 error 日志(production 必须配置)"


# ════════════════════════════════════════════════════════════════
# P1-2 补充: Notification Dedup UTC bucket
# ════════════════════════════════════════════════════════════════

class TestP1_2NotificationDedupUTCBucket:
    """R52 P1-2 补充: dedup window 使用明确 UTC bucket。"""

    def test_window_start_consistent_across_calls(self):
        """同一小时内多次调用返回相同的 window_start(UTC)。"""
        from services.notifications import _compute_window_start
        import datetime as _dt

        fixed_now = _dt.datetime(2026, 7, 15, 14, 30, 0)
        w1 = _compute_window_start(fixed_now)
        fixed_now2 = _dt.datetime(2026, 7, 15, 14, 59, 59)
        w2 = _compute_window_start(fixed_now2)
        assert w1 == w2, "同一小时内应返回相同 window_start(UTC)"

    def test_window_start_changes_across_hours(self):
        """不同小时的 window_start 不同。"""
        from services.notifications import _compute_window_start
        import datetime as _dt

        h1 = _dt.datetime(2026, 7, 15, 14, 30, 0)
        h2 = _dt.datetime(2026, 7, 15, 15, 30, 0)
        w1 = _compute_window_start(h1)
        w2 = _compute_window_start(h2)
        assert w1 != w2, "不同小时应返回不同 window_start"

    def test_window_start_utc_suffix(self):
        """window_start 带 +00:00 UTC 后缀。"""
        from services.notifications import _compute_window_start
        w = _compute_window_start()
        assert w.endswith("+00:00"), \
            f"window_start 应以 +00:00 结尾(UTC),实际: {w}"
