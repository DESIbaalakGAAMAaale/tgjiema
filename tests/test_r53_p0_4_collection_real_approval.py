"""R53 P0-4: Collections bypass 真实审批校验测试。

背景:
    ``services/collections.py`` 中的 ``_update_collection_without_cas()`` 和公共
    bypass 路径只要求 ``approval_action_id`` 非空,没有查询 ``command_executions``
    表,也没有校验 status、principal、request_hash 或 CAS 到 executing,任意非空
    字符串即可绕过乐观锁,审计写入异常还被 warning 后继续。

R53 P0-4 整改:
    - 删除公共 ``bypass_cas`` 参数(彻底移除 bypass 通道)
    - 私有方法 ``_update_collection_without_cas()`` 接收 ``principal_id``、
      ``request_hash``、``target_version`` 参数
    - 调用 ``claim_execution_approved()`` 严格校验:
        * status 必须是 'approved'
        * principal_id 必须匹配审批的 principal
        * request_hash 必须匹配(或前 16 字符匹配)
        * CAS approved→executing
    - 审计失败必须回滚数据修改(用 transaction)
    - 成功回写 ``executed``,失败回写 ``failed``
    - 任意字符串、他人审批、Hash 不符、重复执行全部拒绝(抛 AppError)

测试覆盖 9 个场景:
    1. approval_action_id 为空字符串 → 拒绝(APPROVAL_INVALID)
    2. approval_action_id 为任意字符串"foo" → 拒绝(查不到记录,APPROVAL_INVALID)
    3. approval_action_id 存在但 status != 'approved' → 拒绝(APPROVAL_INVALID)
    4. approval_action_id 存在但 principal_id 不匹配 → 拒绝(PRINCIPAL_MISMATCH)
    5. approval_action_id 存在但 request_hash 不匹配 → 拒绝(HASH_MISMATCH)
    6. approval_action_id 存在且 status='executed' → 拒绝(ALREADY_EXECUTED)
    7. 审批有效 + CAS 成功 → 更新执行,审计写入,状态回写 executed
    8. 审计写入失败 → 数据回滚,状态回写 failed
    9. 审批无效时副作用函数从未被调用(collections 表数据未修改)

测试策略:
    - 使用真实 SQLite 临时数据库(隔离生产数据),通过 ``CacheStore.init()`` 创建表
    - 直接 INSERT 测试数据到 collections / command_executions / audit_log 表
    - 通过 ``unittest.mock.patch`` 模拟 audit_log INSERT 失败(场景 8)
    - 中文注释,英文 raise 消息(遵循项目规范)
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())
sys.modules.setdefault("telegram.ext", __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())

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
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    同时设置 ``_cs_module._store`` 为测试实例,
    使 ``get_cache_store()`` 返回正确的测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r53_p0_4_test_")
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


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _insert_collection(store, owner_id: int, name: str = "test_coll") -> int:
    """插入测试 collections 记录,返回自增 id。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    cursor = await store._db.execute(
        """INSERT INTO collections (name, code, owner_id, description,
                                     version, item_count, status,
                                     created_at, updated_at)
           VALUES (?, ?, ?, '', 1, 0, 'active', ?, ?)""",
        (name, f"code_{name}_{owner_id}", owner_id, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


async def _insert_command_execution(
    store, action_id: str, principal_id: int, status: str = "approved",
    request_hash: str = "a" * 64,
):
    """插入测试 command_executions 记录(用于审批验证)。

    R52 P0-5: 状态机统一为 pending → approved → executing → executed/failed,
    审批通过后执行前的状态为 'approved'。
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


async def _get_command_status(store, action_id: str) -> str | None:
    """查询 command_executions 当前状态。"""
    rows = await store._db.execute_fetchall(
        "SELECT status FROM command_executions WHERE action_id = ?",
        (action_id,),
    )
    return rows[0][0] if rows else None


async def _get_collection_name(store, coll_id: int) -> str | None:
    """查询集合当前 name。"""
    rows = await store._db.execute_fetchall(
        "SELECT name FROM collections WHERE id = ?",
        (coll_id,),
    )
    return rows[0][0] if rows else None


async def _get_audit_log_count(store, coll_id: int) -> int:
    """查询指定集合的 audit_log 记录数。"""
    rows = await store._db.execute_fetchall(
        "SELECT COUNT(*) FROM audit_log WHERE target_id = ? AND action = 'update_collection_bypass'",
        (str(coll_id),),
    )
    return int(rows[0][0]) if rows else 0


# ════════════════════════════════════════════════════════════════
# R53 P0-4: Collections bypass 真实审批校验测试
# ════════════════════════════════════════════════════════════════

class TestR53P0_4CollectionRealApproval:
    """R53 P0-4: _update_collection_without_cas 严格审批校验。"""

    # ── 场景 1: approval_action_id 为空字符串 → 拒绝 ──

    @pytest.mark.asyncio
    async def test_1_empty_approval_action_id_rejected(self, real_store):
        """approval_action_id='' → AppError(COLLECTION_APPROVAL_INVALID)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1001, name="coll_empty")

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="updated",
                principal_id=1001,
                request_hash="a" * 64,
                target_version=1,
                approval_action_id="",  # 空字符串
                caller="test_migration",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_INVALID, (
            f"空 approval_action_id 应抛 APPROVAL_INVALID,实际: {exc_info.value.code}"
        )
        # 校验错误参数
        assert exc_info.value.params.get("approval_action_id") == ""
        assert "approval_action_id_required" in exc_info.value.params.get("reason", "")

    # ── 场景 2: approval_action_id 为任意字符串"foo" → 拒绝(查不到记录) ──

    @pytest.mark.asyncio
    async def test_2_arbitrary_approval_action_id_rejected(self, real_store):
        """approval_action_id='foo'(查不到记录)→ AppError(COLLECTION_APPROVAL_INVALID)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1002, name="coll_foo")

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="updated",
                principal_id=1002,
                request_hash="a" * 64,
                target_version=1,
                approval_action_id="foo",  # 任意字符串,无对应记录
                caller="test_migration",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_INVALID, (
            f"任意字符串应抛 APPROVAL_INVALID,实际: {exc_info.value.code}"
        )
        assert "approval_record_not_found" in exc_info.value.params.get("reason", "")

    # ── 场景 3: approval_action_id 存在但 status != 'approved' → 拒绝 ──

    @pytest.mark.asyncio
    async def test_3_status_not_approved_rejected(self, real_store):
        """status='pending'(非 approved)→ AppError(COLLECTION_APPROVAL_INVALID)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1003, name="coll_pending")
        # 插入 status='pending' 的审批记录
        await _insert_command_execution(
            real_store, "approval_pending_003", principal_id=1003,
            status="pending", request_hash="a" * 64,
        )

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="updated",
                principal_id=1003,
                request_hash="a" * 64,
                target_version=1,
                approval_action_id="approval_pending_003",
                caller="test_migration",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_INVALID, (
            f"status=pending 应抛 APPROVAL_INVALID,实际: {exc_info.value.code}"
        )
        assert "status_not_approved" in exc_info.value.params.get("reason", "")

    # ── 场景 4: approval_action_id 存在但 principal_id 不匹配 → 拒绝 ──

    @pytest.mark.asyncio
    async def test_4_principal_mismatch_rejected(self, real_store):
        """principal_id 不匹配(他人审批)→ AppError(COLLECTION_APPROVAL_PRINCIPAL_MISMATCH)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1004, name="coll_principal")
        # 审批记录的 principal_id=2004(他人审批)
        await _insert_command_execution(
            real_store, "approval_principal_004", principal_id=2004,
            status="approved", request_hash="a" * 64,
        )

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="updated",
                principal_id=1004,  # 调用方 principal_id=1004,与审批记录 2004 不匹配
                request_hash="a" * 64,
                target_version=1,
                approval_action_id="approval_principal_004",
                caller="test_migration",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_PRINCIPAL_MISMATCH, (
            f"principal_id 不匹配应抛 PRINCIPAL_MISMATCH,实际: {exc_info.value.code}"
        )
        # 校验错误参数包含 expected/actual principal_id
        assert exc_info.value.params.get("expected_principal_id") == 1004
        assert exc_info.value.params.get("actual_principal_id") == 2004

    # ── 场景 5: approval_action_id 存在但 request_hash 不匹配 → 拒绝 ──

    @pytest.mark.asyncio
    async def test_5_request_hash_mismatch_rejected(self, real_store):
        """request_hash 不匹配(防篡改)→ AppError(COLLECTION_APPROVAL_HASH_MISMATCH)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1005, name="coll_hash")
        # 审批记录的 request_hash="a"*64(64 位 hex)
        await _insert_command_execution(
            real_store, "approval_hash_005", principal_id=1005,
            status="approved", request_hash="a" * 64,
        )

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="updated",
                principal_id=1005,
                request_hash="b" * 64,  # 篡改的 hash,与存储的 'a'*64 完全不同
                target_version=1,
                approval_action_id="approval_hash_005",
                caller="test_migration",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_HASH_MISMATCH, (
            f"request_hash 不匹配应抛 HASH_MISMATCH,实际: {exc_info.value.code}"
        )

    # ── 场景 6: approval_action_id 存在且 status='executed' → 拒绝(重复执行) ──

    @pytest.mark.asyncio
    async def test_6_already_executed_rejected(self, real_store):
        """status='executed'(已被执行)→ AppError(COLLECTION_APPROVAL_ALREADY_EXECUTED)。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1006, name="coll_executed")
        # 插入 status='executed' 的审批记录(已被执行)
        await _insert_command_execution(
            real_store, "approval_executed_006", principal_id=1006,
            status="executed", request_hash="a" * 64,
        )

        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="updated",
                principal_id=1006,
                request_hash="a" * 64,
                target_version=1,
                approval_action_id="approval_executed_006",
                caller="test_migration",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_ALREADY_EXECUTED, (
            f"status=executed 应抛 ALREADY_EXECUTED,实际: {exc_info.value.code}"
        )

    # ── 场景 7: 审批有效 + CAS 成功 → 更新执行,审计写入,状态回写 executed ──

    @pytest.mark.asyncio
    async def test_7_valid_approval_executes_and_audits(self, real_store):
        """审批有效 + CAS 成功 → collections 更新 + audit_log 写入 + 状态回写 executed。"""
        from services.collections import _update_collection_without_cas

        coll_id = await _insert_collection(real_store, owner_id=1007, name="coll_valid")
        # 插入有效审批记录(status='approved')
        await _insert_command_execution(
            real_store, "approval_valid_007", principal_id=1007,
            status="approved", request_hash="a" * 64,
        )

        # 执行 bypass 更新
        result = await _update_collection_without_cas(
            collection_id=coll_id,
            name="updated_name_007",
            description="updated_desc_007",
            principal_id=1007,
            request_hash="a" * 64,
            target_version=1,
            approval_action_id="approval_valid_007",
            caller="test_migration_007",
        )

        # 校验返回成功
        assert result["success"] is True, f"有效审批应更新成功,实际: {result}"
        assert result["conflict"] is False
        assert result["new_version"] >= 2, "version 应递增"

        # 校验 collections 表 name 已更新
        new_name = await _get_collection_name(real_store, coll_id)
        assert new_name == "updated_name_007", f"name 应为 updated_name_007,实际: {new_name}"

        # 校验 audit_log 表写入了审计记录
        audit_count = await _get_audit_log_count(real_store, coll_id)
        assert audit_count >= 1, f"audit_log 应写入 1 条记录,实际: {audit_count}"

        # 校验 command_executions 状态回写为 'executed'
        final_status = await _get_command_status(real_store, "approval_valid_007")
        assert final_status == "executed", (
            f"审批状态应回写为 executed,实际: {final_status}"
        )

    # ── 场景 8: 审计写入失败 → 数据回滚,状态回写 failed ──

    @pytest.mark.asyncio
    async def test_8_audit_failure_rolls_back_data(self, real_store):
        """审计写入失败 → collections 更新回滚 + 状态回写 failed。"""
        from services.collections import _update_collection_without_cas
        from services.error_codes import AppError, ErrorCodes

        coll_id = await _insert_collection(real_store, owner_id=1008, name="coll_audit_fail")
        original_name = await _get_collection_name(real_store, coll_id)
        assert original_name == "coll_audit_fail"

        # 插入有效审批记录
        await _insert_command_execution(
            real_store, "approval_audit_fail_008", principal_id=1008,
            status="approved", request_hash="a" * 64,
        )

        # 模拟 audit_log INSERT 失败:patch store._db.execute
        # 当 SQL 包含 "INSERT INTO audit_log" 时抛异常
        original_execute = real_store._db.execute

        async def mock_execute_with_audit_failure(sql, params=None):
            if "INSERT INTO audit_log" in sql:
                raise RuntimeError("simulated audit_log INSERT failure")
            return await original_execute(sql, params)

        with patch.object(real_store._db, 'execute', mock_execute_with_audit_failure):
            with pytest.raises(AppError) as exc_info:
                await _update_collection_without_cas(
                    collection_id=coll_id,
                    name="should_not_persist",
                    principal_id=1008,
                    request_hash="a" * 64,
                    target_version=1,
                    approval_action_id="approval_audit_fail_008",
                    caller="test_migration_008",
                )

        # 校验抛出异常(更新失败)
        assert exc_info.value.code == ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED, (
            f"审计失败应抛 VERSION_REQUIRED,实际: {exc_info.value.code}"
        )

        # 校验 collections 表 name 未被修改(事务回滚)
        current_name = await _get_collection_name(real_store, coll_id)
        assert current_name == "coll_audit_fail", (
            f"事务回滚后 name 应保持原值 coll_audit_fail,实际: {current_name}"
        )

        # 校验 command_executions 状态回写为 'failed'
        final_status = await _get_command_status(real_store, "approval_audit_fail_008")
        assert final_status == "failed", (
            f"审计失败后审批状态应回写为 failed,实际: {final_status}"
        )

    # ── 场景 9: 审批无效时副作用从未被调用(collections 表数据未修改) ──

    @pytest.mark.asyncio
    async def test_9_no_side_effects_when_approval_invalid(self, real_store):
        """审批无效(principal_id 不匹配)时,collections 表数据未被修改。"""
        from services.collections import _update_collection_without_cas

        coll_id = await _insert_collection(real_store, owner_id=1009, name="coll_no_side_effect")
        original_name = await _get_collection_name(real_store, coll_id)
        assert original_name == "coll_no_side_effect"

        # 插入审批记录(principal_id=2009,与调用方 1009 不匹配)
        await _insert_command_execution(
            real_store, "approval_no_side_009", principal_id=2009,
            status="approved", request_hash="a" * 64,
        )

        # 调用 bypass(应因 principal_id 不匹配被拒绝)
        from services.error_codes import AppError, ErrorCodes
        with pytest.raises(AppError) as exc_info:
            await _update_collection_without_cas(
                collection_id=coll_id,
                name="should_not_be_applied",
                principal_id=1009,  # 与审批记录 2009 不匹配
                request_hash="a" * 64,
                target_version=1,
                approval_action_id="approval_no_side_009",
                caller="test_migration_009",
            )
        assert exc_info.value.code == ErrorCodes.COLLECTION_APPROVAL_PRINCIPAL_MISMATCH

        # 校验 collections 表 name 未被修改(副作用未执行)
        current_name = await _get_collection_name(real_store, coll_id)
        assert current_name == "coll_no_side_effect", (
            f"审批无效时 name 不应被修改,实际: {current_name}"
        )

        # 校验 audit_log 表无记录(副作用未执行)
        audit_count = await _get_audit_log_count(real_store, coll_id)
        assert audit_count == 0, (
            f"审批无效时 audit_log 不应有记录,实际: {audit_count}"
        )

        # 校验 command_executions 状态仍为 'approved'(未被认领)
        final_status = await _get_command_status(real_store, "approval_no_side_009")
        assert final_status == "approved", (
            f"审批无效时状态应保持 approved(未被认领),实际: {final_status}"
        )


# ════════════════════════════════════════════════════════════════
# 补充: 公共 API 移除 bypass_cas 参数的回归测试
# ════════════════════════════════════════════════════════════════

class TestR53P0_4PublicApiBypassCasRemoved:
    """R53 P0-4: 公共 API update_collection 彻底移除 bypass_cas 参数。"""

    @pytest.mark.asyncio
    async def test_public_api_rejects_bypass_cas_parameter(self, real_store):
        """公共 API 传 bypass_cas=True → TypeError(参数已移除)。"""
        from services.collections import update_collection

        with pytest.raises(TypeError) as exc_info:
            await update_collection(
                collection_id=999,
                name="test",
                expected_version=1,
                bypass_cas=True,  # R53 P0-4: 参数已移除
            )
        assert "bypass_cas" in str(exc_info.value), (
            f"TypeError 应提及 bypass_cas 参数,实际: {exc_info.value}"
        )

    @pytest.mark.asyncio
    async def test_public_api_rejects_approval_action_id_parameter(self, real_store):
        """公共 API 传 approval_action_id → TypeError(参数已移除)。"""
        from services.collections import update_collection

        with pytest.raises(TypeError):
            await update_collection(
                collection_id=999,
                name="test",
                expected_version=1,
                approval_action_id="approval_001",  # R53 P0-4: 参数已移除
            )
