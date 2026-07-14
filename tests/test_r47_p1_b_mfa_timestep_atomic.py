"""R47 P1-b: MFA timestep 原子消费测试。

测试范围(R47 终审报告 P1-b):
- TOTP timestep 原子消费(INSERT OR IGNORE + rowcount 判定重放)
- valid_window=1 记录实际匹配 timestep(遍历 [current-1, current, current+1])
- 不同 timestep 不影响彼此(同一 principal 不同 timestep 可分别消费)
- store 不可用时 fail-closed(_consume_totp_timestep / _is_totp_replayed / _is_locked)
- cleanup_expired_mfa_records(24h retention 清理 mfa_used_totp / mfa_failures)

测试策略:
- 使用临时 SQLite 数据库(real_store fixture)隔离生产数据
- 使用真实 pyotp 生成 code,验证原子消费行为
- 直接调用 _consume_totp_timestep / _find_matching_timestep 验证内部逻辑
- mock store 不可用场景验证 fail-closed
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

# 延迟导入,确保 conftest 已注入 fake config
from database import cache_store as _cs_module
from database.cache_store import CacheStore

# 尝试导入 pyotp,未安装则跳过本模块中需要真实 TOTP 的用例
try:
    import pyotp  # noqa: F401
    _PYOTP_AVAILABLE = True
except ImportError:
    _PYOTP_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parent.parent
MFA_FILE = REPO_ROOT / "admin" / "mfa.py"
SCHEDULER_FILE = REPO_ROOT / "services" / "r40_scheduler.py"


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite 数据库
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    设置 _cs_module._store 使 get_cache_store() 返回测试 store。
    """
    tmpdir = tempfile.mkdtemp(prefix="r47_p1b_test_")
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
def reset_mfa_state():
    """每个用例前重置 MFA 模块级 L1 缓存状态。"""
    from admin import mfa as _mfa_mod
    _mfa_mod.reset_mfa_state_for_testing()
    yield
    _mfa_mod.reset_mfa_state_for_testing()


# ════════════════════════════════════════════════════════════════
# 1. 静态检查:函数定义存在性
# ════════════════════════════════════════════════════════════════


class TestStaticChecks:
    """静态检查:R47 P1-b 新增函数定义存在性。"""

    def test_mfa_has_find_matching_timestep(self):
        """admin/mfa.py 应定义 _find_matching_timestep sync 函数。"""
        import ast
        source = MFA_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        sync_funcs = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        assert "_find_matching_timestep" in sync_funcs, (
            "应定义 _find_matching_timestep sync 函数(R47 P1-b)"
        )

    def test_mfa_has_consume_totp_timestep(self):
        """admin/mfa.py 应定义 _consume_totp_timestep async 函数。"""
        import ast
        source = MFA_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        async_funcs = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        }
        assert "_consume_totp_timestep" in async_funcs, (
            "应定义 _consume_totp_timestep async 函数(R47 P1-b)"
        )

    def test_mfa_has_cleanup_expired_mfa_records(self):
        """admin/mfa.py 应定义 cleanup_expired_mfa_records async 函数。"""
        import ast
        source = MFA_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        async_funcs = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        }
        assert "cleanup_expired_mfa_records" in async_funcs, (
            "应定义 cleanup_expired_mfa_records async 函数(R47 P1-b)"
        )

    def test_mfa_verify_totp_code_uses_consume_totp_timestep(self):
        """verify_totp_code 应调用 _consume_totp_timestep(而非仅 _is_totp_replayed)。"""
        source = MFA_FILE.read_text(encoding="utf-8")
        assert "_consume_totp_timestep" in source, (
            "verify_totp_code 应使用 _consume_totp_timestep 做原子消费"
        )
        assert "_find_matching_timestep" in source, (
            "verify_totp_code 应使用 _find_matching_timestep 查找匹配 timestep"
        )

    def test_mfa_record_mfa_failure_does_not_block(self):
        """_record_mfa_failure 应不阻塞(store 不可用时返回 True 而非 False)。"""
        source = MFA_FILE.read_text(encoding="utf-8")
        # 确认 _record_mfa_failure 中 store 不可用路径返回 True
        assert "不阻塞" in source or "不阻塞" in source, (
            "_record_mfa_failure 应文档化不阻塞行为"
        )

    def test_scheduler_has_cleanup_mfa_job(self):
        """r40_scheduler.py 应定义 cleanup_expired_mfa_records_job。"""
        import ast
        source = SCHEDULER_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        async_funcs = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        }
        assert "cleanup_expired_mfa_records_job" in async_funcs, (
            "r40_scheduler.py 应定义 cleanup_expired_mfa_records_job(R47 P1-b)"
        )

    def test_scheduler_calls_mfa_cleanup(self):
        """r40_scheduler.py 应在 run_scheduler 中调用 cleanup_expired_mfa_records_job。"""
        source = SCHEDULER_FILE.read_text(encoding="utf-8")
        assert "cleanup_expired_mfa_records_job()" in source, (
            "run_scheduler 应调用 cleanup_expired_mfa_records_job()"
        )


# ════════════════════════════════════════════════════════════════
# 2. _find_matching_timestep 单元测试
# ════════════════════════════════════════════════════════════════


class TestFindMatchingTimestep:
    """_find_matching_timestep 行为测试。"""

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    def test_current_timestep_matched(self):
        """当前 timestep 的 code 应返回 current timestep。"""
        from admin.mfa import _find_matching_timestep
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        code = totp.now()
        current_timestep = int(time.time() // 30)
        matched = _find_matching_timestep(secret, code)
        assert matched == current_timestep, (
            f"当前 code 应匹配 current timestep={current_timestep},实际={matched}"
        )

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    def test_prev_timestep_matched(self):
        """上一 timestep 的 code 应返回 prev timestep(valid_window=1 允许 -30s)。"""
        from admin.mfa import _find_matching_timestep
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        current_timestep = int(time.time() // 30)
        prev_timestep = current_timestep - 1
        # 生成 prev timestep 的 code(for_time = prev_timestep * 30)
        prev_code = totp.at(prev_timestep * 30)
        matched = _find_matching_timestep(secret, prev_code)
        assert matched == prev_timestep, (
            f"prev code 应匹配 prev timestep={prev_timestep},实际={matched}"
        )

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    def test_next_timestep_matched(self):
        """下一 timestep 的 code 应返回 next timestep(valid_window=1 允许 +30s)。"""
        from admin.mfa import _find_matching_timestep
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        current_timestep = int(time.time() // 30)
        next_timestep = current_timestep + 1
        next_code = totp.at(next_timestep * 30)
        matched = _find_matching_timestep(secret, next_code)
        assert matched == next_timestep, (
            f"next code 应匹配 next timestep={next_timestep},实际={matched}"
        )

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    def test_far_past_code_returns_none(self):
        """超出 ±30s 窗口的 code 应返回 None(无匹配)。"""
        from admin.mfa import _find_matching_timestep
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        current_timestep = int(time.time() // 30)
        # t-90s 超出 valid_window=1 范围
        far_past_timestep = current_timestep - 3
        far_code = totp.at(far_past_timestep * 30)
        matched = _find_matching_timestep(secret, far_code)
        assert matched is None, "超出 ±30s 的 code 应返回 None"

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    def test_invalid_code_returns_none(self):
        """无效 code 应返回 None。"""
        from admin.mfa import _find_matching_timestep
        secret = pyotp.random_base32()
        matched = _find_matching_timestep(secret, "abcdef")
        assert matched is None, "非数字 code 应返回 None"

    def test_empty_secret_returns_none(self):
        """空 secret 应返回 None(fail-closed)。"""
        from admin.mfa import _find_matching_timestep
        assert _find_matching_timestep("", "123456") is None

    def test_empty_code_returns_none(self):
        """空 code 应返回 None(fail-closed)。"""
        from admin.mfa import _find_matching_timestep
        secret = "ABCDEFGH"  # 任意非空字符串
        assert _find_matching_timestep(secret, "") is None


# ════════════════════════════════════════════════════════════════
# 3. _consume_totp_timestep 原子消费测试
# ════════════════════════════════════════════════════════════════


class TestConsumeTotpTimestep:
    """_consume_totp_timestep 原子消费测试。"""

    @pytest.mark.asyncio
    async def test_first_consume_succeeds(self, real_store):
        """首次消费 timestep 应返回 True(rowcount=1)。"""
        from admin.mfa import _consume_totp_timestep
        principal_id = 41001
        timestep = int(time.time() // 30)
        result = await _consume_totp_timestep(principal_id, timestep)
        assert result is True, "首次消费应返回 True"

    @pytest.mark.asyncio
    async def test_second_consume_same_timestep_fails(self, real_store):
        """同一 timestep 第二次消费应返回 False(rowcount=0,UNIQUE 冲突)。"""
        from admin.mfa import _consume_totp_timestep
        principal_id = 41002
        timestep = int(time.time() // 30)
        # 第一次消费成功
        first = await _consume_totp_timestep(principal_id, timestep)
        assert first is True
        # 第二次消费同一 timestep → UNIQUE 冲突 → False
        second = await _consume_totp_timestep(principal_id, timestep)
        assert second is False, "重复消费同一 timestep 应返回 False(重放)"

    @pytest.mark.asyncio
    async def test_different_timesteps_independent(self, real_store):
        """不同 timestep 互不影响(同一 principal 可分别消费)。"""
        from admin.mfa import _consume_totp_timestep
        principal_id = 41003
        ts1 = int(time.time() // 30)
        ts2 = ts1 + 1
        # 消费 ts1
        r1 = await _consume_totp_timestep(principal_id, ts1)
        assert r1 is True
        # 消费 ts2(不同 timestep,应成功)
        r2 = await _consume_totp_timestep(principal_id, ts2)
        assert r2 is True, "不同 timestep 应独立消费成功"
        # 再次消费 ts1 → 失败(重放)
        r3 = await _consume_totp_timestep(principal_id, ts1)
        assert r3 is False, "再次消费 ts1 应失败(重放)"

    @pytest.mark.asyncio
    async def test_different_principals_independent(self, real_store):
        """不同 principal 的同一 timestep 互不影响。"""
        from admin.mfa import _consume_totp_timestep
        timestep = int(time.time() // 30)
        # principal A 消费 timestep
        r_a = await _consume_totp_timestep(41004, timestep)
        assert r_a is True
        # principal B 消费同一 timestep(应成功,不同 principal)
        r_b = await _consume_totp_timestep(41005, timestep)
        assert r_b is True, "不同 principal 的同一 timestep 应独立消费"

    @pytest.mark.asyncio
    async def test_consume_invalid_params_returns_false(self, real_store):
        """无效参数(principal_id=0 或 timestep=None)应返回 False。"""
        from admin.mfa import _consume_totp_timestep
        assert await _consume_totp_timestep(0, 12345) is False
        assert await _consume_totp_timestep(41006, None) is False

    @pytest.mark.asyncio
    async def test_consume_updates_l1_cache(self, real_store):
        """消费成功后应更新 L1 缓存(_used_totp_codes)。"""
        from admin.mfa import _used_totp_codes, _consume_totp_timestep
        principal_id = 41007
        timestep = int(time.time() // 30)
        await _consume_totp_timestep(principal_id, timestep)
        # L1 缓存应包含该 timestep
        assert principal_id in _used_totp_codes
        assert timestep in _used_totp_codes[principal_id]

    @pytest.mark.asyncio
    async def test_consume_store_unavailable_fail_closed(self):
        """store 不可用时 _consume_totp_timestep 应 fail-closed(返回 False)。"""
        from admin.mfa import _consume_totp_timestep
        # mock _get_store 返回 None(模拟 store 不可用)
        with patch("admin.mfa._get_store", return_value=None):
            result = await _consume_totp_timestep(41008, int(time.time() // 30))
            assert result is False, "store 不可用时应 fail-closed 返回 False"

    @pytest.mark.asyncio
    async def test_consume_db_exception_fail_closed(self, real_store):
        """DB 异常时 _consume_totp_timestep 应 fail-closed(返回 False)。"""
        from admin.mfa import _consume_totp_timestep
        # mock store._db.execute 抛异常
        original_execute = real_store._db.execute
        async def _boom(*args, **kwargs):
            raise Exception("模拟 DB 故障")
        real_store._db.execute = _boom
        try:
            result = await _consume_totp_timestep(41009, int(time.time() // 30))
            assert result is False, "DB 异常时应 fail-closed 返回 False"
        finally:
            real_store._db.execute = original_execute


# ════════════════════════════════════════════════════════════════
# 4. verify_totp_code 端到端原子消费测试
# ════════════════════════════════════════════════════════════════


class TestVerifyTotpCodeAtomicConsume:
    """verify_totp_code 端到端原子消费测试(使用真实 pyotp + 真实 SQLite)。"""

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_same_code_same_timestep_rejected_second_time(self, real_store):
        """同一 code 在同一 timestep 内第二次验证应被拒绝(原子消费)。"""
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        principal_id = 42001
        secret = await manager.generate_totp_secret(principal_id)
        assert secret
        await manager.enable_mfa(principal_id)
        totp = pyotp.TOTP(secret)
        code = totp.now()
        # 第一次验证应通过(消费 timestep)
        ok1 = await manager.verify_totp_code(principal_id, code)
        assert ok1 is True, "第一次验证应通过"
        # 第二次验证同一 code 应被拒绝(timestep 已消费)
        ok2 = await manager.verify_totp_code(principal_id, code)
        assert ok2 is False, "同一 timestep 的 code 重放应被拒绝"

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_different_timestep_codes_independent(self, real_store):
        """不同 timestep 的 code 可分别验证通过(原子消费独立)。"""
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        principal_id = 42002
        secret = await manager.generate_totp_secret(principal_id)
        assert secret
        await manager.enable_mfa(principal_id)
        totp = pyotp.TOTP(secret)
        current_timestep = int(time.time() // 30)
        # 生成 prev timestep 的 code
        prev_code = totp.at((current_timestep - 1) * 30)
        # 生成 current timestep 的 code
        current_code = totp.now()
        # 验证 prev code(应通过,消费 prev timestep)
        ok1 = await manager.verify_totp_code(principal_id, prev_code)
        assert ok1 is True, "prev timestep 的 code 应验证通过"
        # 验证 current code(应通过,不同 timestep)
        ok2 = await manager.verify_totp_code(principal_id, current_code)
        assert ok2 is True, "current timestep 的 code 应验证通过(独立消费)"

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_prev_timestep_code_consumed_correctly(self, real_store):
        """prev timestep 的 code 被消费后,同 code 再次验证应被拒绝。"""
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        principal_id = 42003
        secret = await manager.generate_totp_secret(principal_id)
        assert secret
        await manager.enable_mfa(principal_id)
        totp = pyotp.TOTP(secret)
        current_timestep = int(time.time() // 30)
        prev_code = totp.at((current_timestep - 1) * 30)
        # 第一次验证 prev code(应通过)
        ok1 = await manager.verify_totp_code(principal_id, prev_code)
        assert ok1 is True
        # 第二次验证同一 prev code(应被拒绝,prev timestep 已消费)
        ok2 = await manager.verify_totp_code(principal_id, prev_code)
        assert ok2 is False, "prev timestep 已消费后,同 code 应被拒绝"

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_wrong_code_records_failure(self, real_store):
        """错误 code 应记录失败次数(不触发 timestep 消费)。"""
        from admin.mfa import (
            get_mfa_manager, _record_mfa_failure, _is_locked,
            _MFA_FAIL_MAX_ATTEMPTS,
        )
        manager = get_mfa_manager()
        principal_id = 42004
        secret = await manager.generate_totp_secret(principal_id)
        assert secret
        await manager.enable_mfa(principal_id)
        # 使用明显无效的 code
        ok = await manager.verify_totp_code(principal_id, "abcdef")
        assert ok is False, "无效 code 应验证失败"
        # 失败次数应增加(通过 _is_locked 间接验证:未达阈值不锁定)
        assert not await _is_locked(principal_id), "单次失败不应锁定"

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_success_clears_failures(self, real_store):
        """验证成功后应清除失败计数。"""
        from admin.mfa import get_mfa_manager, _record_mfa_failure, _is_locked
        manager = get_mfa_manager()
        principal_id = 42005
        secret = await manager.generate_totp_secret(principal_id)
        assert secret
        await manager.enable_mfa(principal_id)
        # 记录 3 次失败(未锁定)
        for _ in range(3):
            await _record_mfa_failure(principal_id)
        assert not await _is_locked(principal_id)
        # 验证成功(使用正确 code)
        totp = pyotp.TOTP(secret)
        code = totp.now()
        ok = await manager.verify_totp_code(principal_id, code)
        assert ok is True
        # 失败计数应已清除(查询 SQLite 应为 0)
        store = _cs_module._store
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_failures WHERE principal_id = ?",
            (principal_id,),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 0, "验证成功后 mfa_failures 应清空"


# ════════════════════════════════════════════════════════════════
# 5. fail-closed 测试(store 不可用)
# ════════════════════════════════════════════════════════════════


class TestFailClosedStoreUnavailable:
    """store 不可用时 fail-closed 行为测试。"""

    @pytest.mark.asyncio
    async def test_is_totp_replayed_fail_closed(self):
        """store 不可用时 _is_totp_replayed 应返回 True(视为重放,fail-closed)。"""
        from admin.mfa import _is_totp_replayed
        with patch("admin.mfa._get_store", return_value=None):
            result = await _is_totp_replayed(43001, "123456")
            assert result is True, "store 不可用时 _is_totp_replayed 应 fail-closed"

    @pytest.mark.asyncio
    async def test_is_locked_fail_closed(self):
        """store 不可用时 _is_locked 应返回 True(视为已锁定,fail-closed)。"""
        from admin.mfa import _is_locked
        with patch("admin.mfa._get_store", return_value=None):
            result = await _is_locked(43002)
            assert result is True, "store 不可用时 _is_locked 应 fail-closed"

    @pytest.mark.asyncio
    async def test_record_mfa_failure_does_not_block(self):
        """store 不可用时 _record_mfa_failure 不阻塞(返回 True,仅 warning)。"""
        from admin.mfa import _record_mfa_failure
        with patch("admin.mfa._get_store", return_value=None):
            result = await _record_mfa_failure(43003)
            assert result is True, "_record_mfa_failure 不应阻塞(store 不可用时返回 True)"

    @pytest.mark.asyncio
    async def test_consume_totp_timestep_fail_closed(self):
        """store 不可用时 _consume_totp_timestep 应 fail-closed(返回 False)。"""
        from admin.mfa import _consume_totp_timestep
        with patch("admin.mfa._get_store", return_value=None):
            result = await _consume_totp_timestep(43004, int(time.time() // 30))
            assert result is False, "store 不可用时应 fail-closed 返回 False"

    @pytest.mark.skipif(not _PYOTP_AVAILABLE, reason="pyotp 未安装")
    @pytest.mark.asyncio
    async def test_verify_totp_code_fail_closed_when_store_unavailable(self):
        """store 不可用时 verify_totp_code 应 fail-closed(返回 False)。

        mock _get_store 返回 None,_is_locked 先 fail-closed 返回 True,
        verify_totp_code 直接返回 False。
        """
        from admin.mfa import get_mfa_manager
        manager = get_mfa_manager()
        with patch("admin.mfa._get_store", return_value=None):
            # _is_locked fail-closed → True → verify 直接返回 False
            ok = await manager.verify_totp_code(43005, "123456")
            assert ok is False, "store 不可用时 verify_totp_code 应 fail-closed"


# ════════════════════════════════════════════════════════════════
# 6. cleanup_expired_mfa_records 测试
# ════════════════════════════════════════════════════════════════


class TestCleanupExpiredMfaRecords:
    """cleanup_expired_mfa_records 行为测试。"""

    @pytest.mark.asyncio
    async def test_cleanup_removes_old_used_totp(self, real_store):
        """清理应删除 mfa_used_totp 中超过 retention 的记录。"""
        from admin.mfa import cleanup_expired_mfa_records
        store = _cs_module._store
        principal_id = 44001
        # 插入一条 25 小时前的记录(超过 24h retention)
        old_timestep = int((time.time() - 25 * 3600) // 30)
        old_used_at = time.time() - 25 * 3600
        await store._db.execute(
            "INSERT INTO mfa_used_totp (principal_id, timestep, used_at) VALUES (?, ?, ?)",
            (principal_id, old_timestep, old_used_at),
        )
        await store._db.commit()
        # 插入一条近期记录(应保留)
        recent_timestep = int(time.time() // 30)
        await store._db.execute(
            "INSERT INTO mfa_used_totp (principal_id, timestep, used_at) VALUES (?, ?, ?)",
            (principal_id, recent_timestep, time.time()),
        )
        await store._db.commit()
        # 执行清理(24h retention)
        result = await cleanup_expired_mfa_records(retention_hours=24)
        assert result["deleted_used_totp"] >= 1, "应删除至少 1 条过期 used_totp 记录"
        # 验证近期记录仍存在
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_used_totp WHERE principal_id = ? AND timestep = ?",
            (principal_id, recent_timestep),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, "近期 used_totp 记录应保留"

    @pytest.mark.asyncio
    async def test_cleanup_removes_old_failures(self, real_store):
        """清理应删除 mfa_failures 中超过 retention 的记录。"""
        from admin.mfa import cleanup_expired_mfa_records
        store = _cs_module._store
        principal_id = 44002
        # 插入一条 25 小时前的失败记录(超过 24h retention)
        import datetime as _dt
        old_ms = int((time.time() - 25 * 3600) * 1000)
        await store._db.execute(
            "INSERT INTO mfa_failures (principal_id, failed_at_ms, created_at) "
            "VALUES (?, ?, ?)",
            (principal_id, old_ms, _dt.datetime.now().isoformat()),
        )
        await store._db.commit()
        # 插入一条近期失败记录(应保留)
        recent_ms = int(time.time() * 1000)
        await store._db.execute(
            "INSERT INTO mfa_failures (principal_id, failed_at_ms, created_at) "
            "VALUES (?, ?, ?)",
            (principal_id, recent_ms, _dt.datetime.now().isoformat()),
        )
        await store._db.commit()
        # 执行清理(24h retention)
        result = await cleanup_expired_mfa_records(retention_hours=24)
        assert result["deleted_failures"] >= 1, "应删除至少 1 条过期 failures 记录"
        # 验证近期记录仍存在
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_failures WHERE principal_id = ? AND failed_at_ms = ?",
            (principal_id, recent_ms),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, "近期 failures 记录应保留"

    @pytest.mark.asyncio
    async def test_cleanup_preserves_recent_records(self, real_store):
        """清理应保留 retention 内的记录。"""
        from admin.mfa import cleanup_expired_mfa_records
        store = _cs_module._store
        principal_id = 44003
        # 插入近期 used_totp 记录
        recent_timestep = int(time.time() // 30)
        await store._db.execute(
            "INSERT INTO mfa_used_totp (principal_id, timestep, used_at) VALUES (?, ?, ?)",
            (principal_id, recent_timestep, time.time()),
        )
        await store._db.commit()
        # 执行清理
        result = await cleanup_expired_mfa_records(retention_hours=24)
        # 近期记录不应被删除
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_used_totp WHERE principal_id = ?",
            (principal_id,),
        )
        row = await cursor.fetchone()
        assert int(row[0]) == 1, "近期记录应保留"

    @pytest.mark.asyncio
    async def test_cleanup_empty_store_returns_zeros(self, real_store):
        """空表清理应返回 0(无错误)。"""
        from admin.mfa import cleanup_expired_mfa_records
        result = await cleanup_expired_mfa_records(retention_hours=24)
        assert result["deleted_used_totp"] == 0
        assert result["deleted_failures"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_store_unavailable_returns_zeros(self):
        """store 不可用时 cleanup 应返回 0(不抛异常)。"""
        from admin.mfa import cleanup_expired_mfa_records
        with patch("admin.mfa._get_store", return_value=None):
            result = await cleanup_expired_mfa_records(retention_hours=24)
            assert result["deleted_used_totp"] == 0
            assert result["deleted_failures"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_custom_retention(self, real_store):
        """自定义 retention_hours 应正确过滤。"""
        from admin.mfa import cleanup_expired_mfa_records
        store = _cs_module._store
        principal_id = 44004
        # 插入 2 小时前的记录
        old_used_at = time.time() - 2 * 3600
        old_timestep = int((time.time() - 2 * 3600) // 30)
        await store._db.execute(
            "INSERT INTO mfa_used_totp (principal_id, timestep, used_at) VALUES (?, ?, ?)",
            (principal_id, old_timestep, old_used_at),
        )
        await store._db.commit()
        # 用 1 小时 retention 清理(2 小时前的记录应被删除)
        result = await cleanup_expired_mfa_records(retention_hours=1)
        assert result["deleted_used_totp"] >= 1, "1h retention 应删除 2h 前的记录"
        # 用 3 小时 retention 清理(无更多记录可删)
        result2 = await cleanup_expired_mfa_records(retention_hours=3)
        assert result2["deleted_used_totp"] == 0, "3h retention 不应再删除"


# ════════════════════════════════════════════════════════════════
# 7. _record_mfa_failure 不阻塞测试
# ════════════════════════════════════════════════════════════════


class TestRecordMfaFailureNonBlocking:
    """_record_mfa_failure 不阻塞行为测试(R47 P1-b)。"""

    @pytest.mark.asyncio
    async def test_record_failure_store_unavailable_returns_true(self):
        """store 不可用时 _record_mfa_failure 返回 True(不阻塞)。"""
        from admin.mfa import _record_mfa_failure
        with patch("admin.mfa._get_store", return_value=None):
            result = await _record_mfa_failure(45001)
            assert result is True, "store 不可用时 _record_mfa_failure 应返回 True(不阻塞)"

    @pytest.mark.asyncio
    async def test_record_failure_db_exception_returns_true(self, real_store):
        """DB 异常时 _record_mfa_failure 返回 True(不阻塞)。"""
        from admin.mfa import _record_mfa_failure
        original_execute = real_store._db.execute
        async def _boom(*args, **kwargs):
            raise Exception("模拟 DB 故障")
        real_store._db.execute = _boom
        try:
            result = await _record_mfa_failure(45002)
            assert result is True, "DB 异常时 _record_mfa_failure 应返回 True(不阻塞)"
        finally:
            real_store._db.execute = original_execute

    @pytest.mark.asyncio
    async def test_record_failure_invalid_principal_returns_true(self, real_store):
        """principal_id=0 时 _record_mfa_failure 返回 True(不阻塞)。"""
        from admin.mfa import _record_mfa_failure
        result = await _record_mfa_failure(0)
        assert result is True, "principal_id=0 时应返回 True(不阻塞)"

    @pytest.mark.asyncio
    async def test_record_failure_success_returns_true(self, real_store):
        """正常写入成功时 _record_mfa_failure 返回 True。"""
        from admin.mfa import _record_mfa_failure
        result = await _record_mfa_failure(45003)
        assert result is True
        # 验证写入成功
        store = _cs_module._store
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM mfa_failures WHERE principal_id = ?",
            (45003,),
        )
        row = await cursor.fetchone()
        assert int(row[0]) >= 1, "失败记录应已写入 SQLite"
