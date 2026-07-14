"""R47 P1-E Up Bot 启动扫描整改测试(终审报告 17 节 - Up Bot 审查)。

测试覆盖 5 个场景:
1. 启动扫描补写 Manifest(模拟未注册副本 → 补写 → 标记 reconciled)
2. _register_manifest 异常不吞(raise 而非静默)
3. unregistered_copies 写入失败触发 reconcile(fail-closed + audit_log)
4. 重启后已 reconciled 的不重复补写
5. 部分成功后重启只补写失败的

测试策略:
- 真实 SQLite 临时文件数据库(隔离生产数据)
- 直接设置 _channel_to_group 映射(避免依赖 cells 表)
- 直接调用 _reconcile_unregistered_copies / _register_manifest / _mark_copied_unregistered
- 通过控制 unregistered_copies 表数据模拟"强杀进程"场景
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

# ── Python 3.9 兼容:Mock 使用 3.10+ 类型注解语法的模块 ──────────
def _install_type_annotation_mocks_if_needed() -> None:
    """注入使用 Python 3.10+ 类型注解语法的 utils 模块的 mock。

    仅在真实模块导入失败时生效(避免覆盖 Python 3.10+ 环境)。
    """
    # 确保 telegram.error 子模块可用
    if "telegram" in sys.modules and not inspect.ismodule(sys.modules["telegram"]):
        if "telegram.error" not in sys.modules:
            mock_err = types.ModuleType("telegram.error")
            mock_err.NetworkError = type("NetworkError", (Exception,), {})
            mock_err.TimedOut = type("TimedOut", (Exception,), {})
            mock_err.BadRequest = type("BadRequest", (Exception,), {})
            mock_err.Forbidden = type("Forbidden", (Exception,), {})
            mock_err.RetryAfter = type("RetryAfter", (Exception,), {})
            sys.modules["telegram.error"] = mock_err

    _problem_modules = [
        "utils.rate_limiter",
        "utils.dynamic_rate_limiter",
        "utils.storage_channel",
        "utils.admin_notify",
    ]
    for mod_name in _problem_modules:
        try:
            importlib.import_module(mod_name)
        except Exception:
            sys.modules.pop(mod_name, None)
            mock_mod = types.ModuleType(mod_name)
            if mod_name == "utils.rate_limiter":
                mock_mod.global_rate_limiter = MagicMock()
                mock_mod.global_rate_limiter.acquire = AsyncMock(return_value=True)
                mock_mod.user_rate_limiter = MagicMock()
                mock_mod.user_rate_limiter.acquire = AsyncMock(return_value=True)
                mock_mod.RateLimiter = MagicMock
                mock_mod.UserRateLimiter = MagicMock
            elif mod_name == "utils.dynamic_rate_limiter":
                mock_mod.dynamic_rate_limiter = MagicMock()
                mock_mod.dynamic_rate_limiter.acquire = AsyncMock(return_value=None)
                mock_mod.DynamicRateLimiter = MagicMock
            sys.modules[mod_name] = mock_mod


import importlib
_install_type_annotation_mocks_if_needed()

# 补充 conftest MagicMock settings 缺失的 RATE_LIMIT_* 属性
try:
    from config import settings as _settings_for_rl
    if not hasattr(_settings_for_rl, "RATE_LIMIT_THRESHOLD_HIGH") or \
            not isinstance(_settings_for_rl.RATE_LIMIT_THRESHOLD_HIGH, (int, float)):
        _settings_for_rl.RATE_LIMIT_THRESHOLD_HIGH = 100
    if not hasattr(_settings_for_rl, "RATE_LIMIT_THRESHOLD_LOW") or \
            not isinstance(_settings_for_rl.RATE_LIMIT_THRESHOLD_LOW, (int, float)):
        _settings_for_rl.RATE_LIMIT_THRESHOLD_LOW = 10
    if not hasattr(_settings_for_rl, "RATE_LIMIT_BASE_DELAY") or \
            not isinstance(_settings_for_rl.RATE_LIMIT_BASE_DELAY, (int, float)):
        _settings_for_rl.RATE_LIMIT_BASE_DELAY = 0.0
    if not hasattr(_settings_for_rl, "RATE_LIMIT_MAX_DELAY") or \
            not isinstance(_settings_for_rl.RATE_LIMIT_MAX_DELAY, (int, float)):
        _settings_for_rl.RATE_LIMIT_MAX_DELAY = 60.0
except Exception:
    pass

# 导入被测模块
from bots import up_bot


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite 数据库
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r47_p1_e_test_")
    db_path = Path(tmpdir) / "test_r47_p1_e.db"
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
async def channel_map(store):
    """设置 _channel_to_group 映射(避免依赖 cells 表)。

    映射 channel_id=200 → group_id=1, channel_id=300 → group_id=2。
    同时设置 _channel_to_group_ts 为当前时间,使 _ensure_channel_group_map
    认为缓存有效而直接返回(不刷新)。
    """
    original_map = dict(up_bot._channel_to_group)
    original_ts = up_bot._channel_to_group_ts
    up_bot._channel_to_group = {200: 1, 300: 2}
    up_bot._channel_to_group_ts = time.time()
    try:
        yield
    finally:
        up_bot._channel_to_group = original_map
        up_bot._channel_to_group_ts = original_ts


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


async def _insert_unregistered_copy(
    store, upload_id: str, fuid: str, channel_id: int,
    message_id: int, media_group_id: str = "", reason: str = "test",
):
    """直接向 unregistered_copies 表插入一条未对账记录。"""
    await store.insert_unregistered_copy(
        upload_id=upload_id,
        file_unique_id=fuid,
        channel_id=channel_id,
        message_id=message_id,
        media_group_id=media_group_id,
        reason=reason,
    )


async def _count_unreconciled(store) -> int:
    """统计 unregistered_copies 表中未对账行数。"""
    copies = await store.list_unreconciled_copies(limit=10000)
    return len(copies)


async def _count_manifest_rows(store, group_id: int) -> int:
    """统计 manifest 表中指定 group_id 的行数。"""
    rows = await store.get_manifest_by_group(group_id)
    return len(rows)


async def _count_audit_log_entries(store, action: str = "unregistered_copy_persist_failed") -> int:
    """统计 audit_log 中指定 action 的行数。"""
    if not store._db:
        return 0
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = ?",
        (action,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


# ════════════════════════════════════════════════════════════════
# 场景 1: 启动扫描补写 Manifest(模拟未注册副本 → 补写 → 标记 reconciled)
# ════════════════════════════════════════════════════════════════


class TestReconcileWritesManifest:
    """启动扫描 _reconcile_unregistered_copies 实际补写 Manifest 并标记 reconciled。"""

    @pytest.mark.asyncio
    async def test_reconcile_writes_manifest_and_marks_reconciled(self, store, channel_map):
        """未注册副本 → 启动扫描补写 Manifest → 标记 reconciled。"""
        # 准备:插入 1 条未对账副本
        await _insert_unregistered_copy(
            store, upload_id="r47-001", fuid="fuid-001",
            channel_id=200, message_id=10001,
            media_group_id="mg-001", reason="outbox_write_failed",
        )
        assert await _count_unreconciled(store) == 1
        assert await _count_manifest_rows(store, 1) == 0

        # 执行启动扫描
        stats = await up_bot._reconcile_unregistered_copies()

        # 验证:统计字典
        assert stats["total"] == 1
        assert stats["reconciled"] == 1
        assert stats["failed"] == 0

        # 验证:Manifest 已补写
        assert await _count_manifest_rows(store, 1) == 1

        # 验证:unregistered_copies 已标记 reconciled(不再出现在未对账列表)
        assert await _count_unreconciled(store) == 0

    @pytest.mark.asyncio
    async def test_reconcile_multiple_copies(self, store, channel_map):
        """多条未注册副本 → 全部补写 Manifest → 全部标记 reconciled。"""
        # 准备:插入 3 条未对账副本(2 个 channel 200/group 1, 1 个 channel 300/group 2)
        await _insert_unregistered_copy(
            store, upload_id="r47-002", fuid="fuid-002",
            channel_id=200, message_id=10002, reason="test",
        )
        await _insert_unregistered_copy(
            store, upload_id="r47-003", fuid="fuid-003",
            channel_id=200, message_id=10003, reason="test",
        )
        await _insert_unregistered_copy(
            store, upload_id="r47-004", fuid="fuid-004",
            channel_id=300, message_id=10004, reason="test",
        )
        assert await _count_unreconciled(store) == 3

        # 执行启动扫描
        stats = await up_bot._reconcile_unregistered_copies()

        # 验证:全部成功
        assert stats["total"] == 3
        assert stats["reconciled"] == 3
        assert stats["failed"] == 0

        # 验证:Manifest 已补写(group 1 有 2 条, group 2 有 1 条)
        assert await _count_manifest_rows(store, 1) == 2
        assert await _count_manifest_rows(store, 2) == 1

        # 验证:全部标记 reconciled
        assert await _count_unreconciled(store) == 0

    @pytest.mark.asyncio
    async def test_reconcile_no_copies_returns_zero(self, store, channel_map):
        """无未对账副本时返回 total=0。"""
        stats = await up_bot._reconcile_unregistered_copies()
        assert stats["total"] == 0
        assert stats["reconciled"] == 0
        assert stats["failed"] == 0


# ════════════════════════════════════════════════════════════════
# 场景 2: _register_manifest 异常不吞(raise 而非静默)
# ════════════════════════════════════════════════════════════════


class TestRegisterManifestRaisesOnFailure:
    """_register_manifest 异常向上传播(不吞异常)。"""

    @pytest.mark.asyncio
    async def test_raises_when_group_id_not_found(self, store):
        """频道未映射到 group_id 时 raise RuntimeError(不静默 return)。"""
        # 确保频道 999 不在映射中
        original_map = dict(up_bot._channel_to_group)
        original_ts = up_bot._channel_to_group_ts
        up_bot._channel_to_group = {200: 1}
        up_bot._channel_to_group_ts = time.time()
        try:
            with pytest.raises(RuntimeError, match="无法解析频道 999"):
                await up_bot._register_manifest(
                    channel_id=999,
                    message_id=10001,
                    msg=None,
                    file_unique_id_override="fuid-err-001",
                )
        finally:
            up_bot._channel_to_group = original_map
            up_bot._channel_to_group_ts = original_ts

    @pytest.mark.asyncio
    async def test_raises_when_upsert_manifest_fails(self, store, channel_map):
        """upsert_manifest 失败时异常向上传播(不吞异常)。"""
        # Mock store.upsert_manifest 抛异常
        original_upsert = store.upsert_manifest
        store.upsert_manifest = AsyncMock(side_effect=RuntimeError("db locked"))
        try:
            with pytest.raises(RuntimeError, match="db locked"):
                await up_bot._register_manifest(
                    channel_id=200,
                    message_id=10001,
                    msg=None,
                    file_unique_id_override="fuid-err-002",
                )
        finally:
            store.upsert_manifest = original_upsert

    @pytest.mark.asyncio
    async def test_no_silent_pass_in_register_manifest(self):
        """静态检查:_register_manifest 源码不应包含 except Exception: pass。"""
        source = inspect.getsource(up_bot._register_manifest)
        # 不应包含静默吞异常的 pass
        assert "except Exception:" not in source or "pass  #" not in source, (
            "_register_manifest 不应包含 `except Exception: pass` 吞异常路径"
        )
        # 不应包含 except Exception as e: 后跟 return(静默返回)
        assert "logger.warning" not in source or "跳过 manifest 登记" not in source, (
            "_register_manifest 不应包含 logger.warning + return 的吞异常路径"
        )


# ════════════════════════════════════════════════════════════════
# 场景 3: unregistered_copies 写入失败触发 reconcile(fail-closed + audit_log)
# ════════════════════════════════════════════════════════════════


class TestMarkCopiedUnregisteredFailClosed:
    """_mark_copied_unregistered 持久化失败时 fail-closed(写 audit_log + 返回 partial_success)。"""

    @pytest.mark.asyncio
    async def test_returns_partial_success_when_persist_fails(self, store, channel_map):
        """insert_unregistered_copy 失败时返回 partial_success(不静默吞异常)。"""
        # Mock insert_unregistered_copy 抛异常
        original_insert = store.insert_unregistered_copy
        store.insert_unregistered_copy = AsyncMock(side_effect=RuntimeError("sqlite disk full"))
        up_bot._media_group_states.clear()
        try:
            result = await up_bot._mark_copied_unregistered(
                upload_id="r47-fail-001",
                media_group_id="mg-fail-001",
                file_unique_id="fuid-fail-001",
                message_id=20001,
                channel_id=200,
                reason="outbox_write_failed",
            )
            # 验证:返回 partial_success(不是 None)
            assert result == "partial_success", (
                "持久化失败时应返回 partial_success,不静默吞异常"
            )
        finally:
            store.insert_unregistered_copy = original_insert

    @pytest.mark.asyncio
    async def test_writes_audit_log_on_persist_failure(self, store, channel_map):
        """持久化失败时写入 audit_log(供运维人工 reconcile)。"""
        # Mock insert_unregistered_copy 抛异常
        original_insert = store.insert_unregistered_copy
        store.insert_unregistered_copy = AsyncMock(side_effect=RuntimeError("sqlite disk full"))
        up_bot._media_group_states.clear()
        try:
            await up_bot._mark_copied_unregistered(
                upload_id="r47-fail-002",
                media_group_id="mg-fail-002",
                file_unique_id="fuid-fail-002",
                message_id=20002,
                channel_id=200,
                reason="outbox_write_failed",
            )
            # 验证:audit_log 已写入
            count = await _count_audit_log_entries(store)
            assert count >= 1, "持久化失败时应写入 audit_log"
        finally:
            store.insert_unregistered_copy = original_insert

    @pytest.mark.asyncio
    async def test_returns_persisted_on_success(self, store, channel_map):
        """持久化成功时返回 persisted。"""
        up_bot._media_group_states.clear()
        result = await up_bot._mark_copied_unregistered(
            upload_id="r47-ok-001",
            media_group_id="mg-ok-001",
            file_unique_id="fuid-ok-001",
            message_id=20003,
            channel_id=200,
            reason="outbox_write_failed",
        )
        assert result == "persisted"
        # 验证:unregistered_copies 表有 1 条未对账行
        assert await _count_unreconciled(store) == 1


# ════════════════════════════════════════════════════════════════
# 场景 4: 重启后已 reconciled 的不重复补写
# ════════════════════════════════════════════════════════════════


class TestNoDuplicateOnRestart:
    """重启后已 reconciled 的不重复补写(幂等性)。"""

    @pytest.mark.asyncio
    async def test_reconciled_rows_not_reprocessed(self, store, channel_map):
        """已 reconciled 的行不在下次启动扫描中被重复补写。"""
        # 准备:插入 1 条未对账副本
        await _insert_unregistered_copy(
            store, upload_id="r47-restart-001", fuid="fuid-restart-001",
            channel_id=200, message_id=10001, reason="test",
        )

        # 第一次启动扫描:补写 Manifest + 标记 reconciled
        stats1 = await up_bot._reconcile_unregistered_copies()
        assert stats1["total"] == 1
        assert stats1["reconciled"] == 1
        assert await _count_manifest_rows(store, 1) == 1

        # 第二次启动扫描(模拟重启):无未对账行,不重复补写
        stats2 = await up_bot._reconcile_unregistered_copies()
        assert stats2["total"] == 0
        assert stats2["reconciled"] == 0

        # 验证:Manifest 仍只有 1 条(未重复补写)
        assert await _count_manifest_rows(store, 1) == 1

    @pytest.mark.asyncio
    async def test_manifest_upsert_is_idempotent(self, store, channel_map):
        """Manifest upsert 幂等:重复补写同一行不创建重复记录。"""
        # 准备:插入 1 条未对账副本
        await _insert_unregistered_copy(
            store, upload_id="r47-idem-001", fuid="fuid-idem-001",
            channel_id=200, message_id=10001, reason="test",
        )

        # 第一次启动扫描
        await up_bot._reconcile_unregistered_copies()
        assert await _count_manifest_rows(store, 1) == 1

        # 手动重新插入一条相同的未对账行(模拟 reconciled 标记失败但 Manifest 已写)
        await _insert_unregistered_copy(
            store, upload_id="r47-idem-001", fuid="fuid-idem-001",
            channel_id=200, message_id=10001, reason="test_retry",
        )
        # INSERT OR IGNORE 不会插入重复行(PK 冲突),但 reconciled_at 被重置
        # 实际上由于 PK 冲突,INSERT OR IGNORE 会忽略,行仍是 reconciled 状态
        # 验证:第二次扫描应发现 0 条未对账行(PK 冲突导致未插入)
        stats2 = await up_bot._reconcile_unregistered_copies()
        # 由于 INSERT OR IGNORE,重复行不会插入,reconciled_at 不会被重置
        assert stats2["total"] == 0

        # Manifest 仍只有 1 条
        assert await _count_manifest_rows(store, 1) == 1


# ════════════════════════════════════════════════════════════════
# 场景 5: 部分成功后重启只补写失败的
# ════════════════════════════════════════════════════════════════


class TestPartialSuccessRetryOnRestart:
    """部分成功后重启只补写失败的(失败行不标记 reconciled,下次重试)。"""

    @pytest.mark.asyncio
    async def test_failed_rows_not_marked_reconciled(self, store):
        """_register_manifest 失败的行不标记 reconciled,下次启动可重试。"""
        # 准备:channel 200 映射到 group 1,channel 999 不映射
        original_map = dict(up_bot._channel_to_group)
        original_ts = up_bot._channel_to_group_ts
        up_bot._channel_to_group = {200: 1}  # 只有 200 映射,999 不映射
        up_bot._channel_to_group_ts = time.time()
        try:
            # 插入 2 条未对账副本:1 条可补写(channel 200),1 条不可(channel 999)
            await _insert_unregistered_copy(
                store, upload_id="r47-partial-001", fuid="fuid-partial-ok",
                channel_id=200, message_id=10001, reason="test",
            )
            await _insert_unregistered_copy(
                store, upload_id="r47-partial-002", fuid="fuid-partial-fail",
                channel_id=999, message_id=10002, reason="test",
            )
            assert await _count_unreconciled(store) == 2

            # 第一次启动扫描:1 成功 1 失败
            stats1 = await up_bot._reconcile_unregistered_copies()
            assert stats1["total"] == 2
            assert stats1["reconciled"] == 1
            assert stats1["failed"] == 1

            # 验证:仍有 1 条未对账(失败的行)
            assert await _count_unreconciled(store) == 1

            # 验证:Manifest 只补写了成功的 1 条
            assert await _count_manifest_rows(store, 1) == 1
        finally:
            up_bot._channel_to_group = original_map
            up_bot._channel_to_group_ts = original_ts

    @pytest.mark.asyncio
    async def test_retry_failed_row_after_mapping_fixed(self, store):
        """修复映射后重启,失败的行被补写(模拟映射刷新后重试成功)。"""
        # 第一次启动:channel 999 不在映射中 → 失败
        original_map = dict(up_bot._channel_to_group)
        original_ts = up_bot._channel_to_group_ts
        up_bot._channel_to_group = {200: 1}
        up_bot._channel_to_group_ts = time.time()
        try:
            await _insert_unregistered_copy(
                store, upload_id="r47-retry-001", fuid="fuid-retry-001",
                channel_id=999, message_id=10001, reason="test",
            )

            stats1 = await up_bot._reconcile_unregistered_copies()
            assert stats1["total"] == 1
            assert stats1["reconciled"] == 0
            assert stats1["failed"] == 1
            assert await _count_unreconciled(store) == 1

            # 模拟映射刷新:channel 999 现在映射到 group 3
            up_bot._channel_to_group = {200: 1, 999: 3}
            up_bot._channel_to_group_ts = time.time()

            # 第二次启动扫描(模拟重启):失败的行被补写
            stats2 = await up_bot._reconcile_unregistered_copies()
            assert stats2["total"] == 1
            assert stats2["reconciled"] == 1
            assert stats2["failed"] == 0

            # 验证:Manifest 已补写(group 3 有 1 条)
            assert await _count_manifest_rows(store, 3) == 1

            # 验证:全部标记 reconciled
            assert await _count_unreconciled(store) == 0
        finally:
            up_bot._channel_to_group = original_map
            up_bot._channel_to_group_ts = original_ts

    @pytest.mark.asyncio
    async def test_mixed_success_failure_only_failed_remain(self, store):
        """混合场景:3 条未对账,2 成功 1 失败,重启后只补写失败的 1 条。"""
        original_map = dict(up_bot._channel_to_group)
        original_ts = up_bot._channel_to_group_ts
        up_bot._channel_to_group = {200: 1, 300: 2}  # 888 不映射
        up_bot._channel_to_group_ts = time.time()
        try:
            # 插入 3 条:2 条可补写(200, 300),1 条不可(888)
            await _insert_unregistered_copy(
                store, upload_id="r47-mix-001", fuid="fuid-mix-ok1",
                channel_id=200, message_id=10001, reason="test",
            )
            await _insert_unregistered_copy(
                store, upload_id="r47-mix-002", fuid="fuid-mix-ok2",
                channel_id=300, message_id=10002, reason="test",
            )
            await _insert_unregistered_copy(
                store, upload_id="r47-mix-003", fuid="fuid-mix-fail",
                channel_id=888, message_id=10003, reason="test",
            )

            # 第一次启动扫描:2 成功 1 失败
            stats1 = await up_bot._reconcile_unregistered_copies()
            assert stats1["total"] == 3
            assert stats1["reconciled"] == 2
            assert stats1["failed"] == 1
            assert await _count_unreconciled(store) == 1

            # 修复映射:channel 888 → group 4
            up_bot._channel_to_group = {200: 1, 300: 2, 888: 4}
            up_bot._channel_to_group_ts = time.time()

            # 第二次启动扫描:只补写失败的 1 条
            stats2 = await up_bot._reconcile_unregistered_copies()
            assert stats2["total"] == 1
            assert stats2["reconciled"] == 1
            assert stats2["failed"] == 0

            # 验证:全部标记 reconciled
            assert await _count_unreconciled(store) == 0

            # 验证:Manifest 总共 3 条(group 1: 1, group 2: 1, group 4: 1)
            assert await _count_manifest_rows(store, 1) == 1
            assert await _count_manifest_rows(store, 2) == 1
            assert await _count_manifest_rows(store, 4) == 1
        finally:
            up_bot._channel_to_group = original_map
            up_bot._channel_to_group_ts = original_ts
