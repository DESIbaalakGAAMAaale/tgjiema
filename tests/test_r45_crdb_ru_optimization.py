"""R45: CRDB RU 与索引维护优化测试。

测试覆盖:
    - database.session.DDL_VERSION == 11
    - database.session.MIGRATION_STATEMENTS 包含 pending_uploads 部分索引迁移语句
    - database.session.DDL_STATEMENTS 包含 pending_uploads 部分索引(全新部署)
    - services.decode_logs_cleanup.cleanup_expired_decode_logs 批量删除逻辑(mock store)
    - services.decode_logs_cleanup.cleanup_expired_decode_logs 批次限制(max_batches)
    - services.decode_logs_cleanup.cleanup_expired_decode_logs 异常处理
    - services.decode_logs_cleanup.cleanup_expired_decode_logs 自定义保留天数
    - services.decode_logs_cleanup.run_daily_cleanup_loop 循环逻辑
    - scripts/verify_file_records_status_index.py 脚本存在且包含关键函数
    - services.r40_scheduler.cleanup_expired_decode_logs_job 函数存在
    - services.r40_scheduler 源码引用 decode_logs 清理(create_safe_task + 主循环调度)

测试策略:
    - 全部使用 mock,不依赖真实 SQLite / CRDB
    - 中文注释,与项目其他测试保持一致
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试文件顶部 mock telegram 模块(避免 import 失败)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ─── 1. database.session 部分索引迁移测试 ──────────────────────────

def test_ddl_version_is_11():
    """R45: DDL_VERSION 应升级到 11。"""
    from database import session
    assert session.DDL_VERSION == 11


def test_migration_statements_contain_pending_uploads_partial_index():
    """R45: MIGRATION_STATEMENTS 应包含 pending_uploads 部分索引迁移语句。"""
    from database import session
    # 合并多行字符串字面量(SQL "CREATE INDEX ... ON ..." 跨行拼接)
    joined = " ".join(session.MIGRATION_STATEMENTS)
    # 部分索引: DROP 旧索引
    assert "DROP INDEX IF EXISTS idx_pending_uploads_unprocessed" in joined
    # 部分索引: CREATE 新索引(仅 processed=0)
    assert "idx_pending_uploads_pending_created" in joined
    assert "WHERE processed = 0" in joined
    # reclaim 索引(claimed_at, created_at)
    assert "idx_pending_uploads_reclaim" in joined


def test_ddl_statements_contain_partial_index_for_new_deploy():
    """R45: DDL_STATEMENTS 应包含部分索引(全新部署直接创建部分索引)。"""
    from database import session
    joined = " ".join(session.DDL_STATEMENTS)
    # 全新部署直接创建部分索引
    assert "idx_pending_uploads_pending_created" in joined
    assert "WHERE processed = 0" in joined
    # 旧的 idx_pending_uploads_unprocessed 不应再出现在 DDL_STATEMENTS
    assert "idx_pending_uploads_unprocessed ON pending_uploads(processed)" not in joined


# ─── 2. decode_logs_cleanup 批量删除测试 ──────────────────────────

class _FakeCursor:
    """模拟 aiosqlite cursor,支持 rowcount。"""
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeDb:
    """模拟 store._db,记录 execute 调用并按预设序列返回 rowcount。"""
    def __init__(self, rowcounts, raise_on_call=None):
        self._rowcounts = list(rowcounts)
        self._call_idx = 0
        self._raise_on_call = raise_on_call
        self.execute_calls = []
        self.commit_calls = 0

    async def execute(self, sql, params):
        self.execute_calls.append((sql, params))
        idx = self._call_idx
        self._call_idx += 1
        if self._raise_on_call is not None and idx == self._raise_on_call:
            raise RuntimeError(f"mock execute error at call {idx}")
        rc = self._rowcounts[idx] if idx < len(self._rowcounts) else 0
        return _FakeCursor(rc)

    async def commit(self):
        self.commit_calls += 1


class _FakeStore:
    """模拟 cache_store,包含 _db 属性。"""
    def __init__(self, db):
        self._db = db


@pytest.mark.asyncio
async def test_cleanup_expired_decode_logs_batch_deletion():
    """R45: cleanup_expired_decode_logs 应批量删除过期记录直到清空。"""
    from services.decode_logs_cleanup import cleanup_expired_decode_logs
    # 模拟: 第 1 批删除 500(满),第 2 批删除 300(不足,停止)
    db = _FakeDb(rowcounts=[500, 300])
    store = _FakeStore(db)
    result = await cleanup_expired_decode_logs(store, retention_days=7, batch_size=500, max_batches=20)
    assert result["deleted_count"] == 800
    assert result["batches_run"] == 2
    assert result["retention_days"] == 7
    assert "cutoff_time" in result
    # 应执行 2 次 execute(每批一次)
    assert len(db.execute_calls) == 2
    # 每次 execute 后都 commit
    assert db.commit_calls == 2


@pytest.mark.asyncio
async def test_cleanup_expired_decode_logs_max_batches_limit():
    """R45: 当持续满批时,应受 max_batches 限制停止。"""
    from services.decode_logs_cleanup import cleanup_expired_decode_logs
    # 模拟: 每批都满 500,最多 3 批
    db = _FakeDb(rowcounts=[500, 500, 500])
    store = _FakeStore(db)
    result = await cleanup_expired_decode_logs(store, retention_days=7, batch_size=500, max_batches=3)
    assert result["deleted_count"] == 1500
    assert result["batches_run"] == 3
    assert len(db.execute_calls) == 3


@pytest.mark.asyncio
async def test_cleanup_expired_decode_logs_empty():
    """R45: 无过期记录时,第 1 批删除 0 即停止。"""
    from services.decode_logs_cleanup import cleanup_expired_decode_logs
    db = _FakeDb(rowcounts=[0])
    store = _FakeStore(db)
    result = await cleanup_expired_decode_logs(store, retention_days=7, batch_size=500, max_batches=20)
    assert result["deleted_count"] == 0
    assert result["batches_run"] == 1
    assert len(db.execute_calls) == 1


@pytest.mark.asyncio
async def test_cleanup_expired_decode_logs_exception_handling():
    """R45: execute 抛异常时,应中断循环并返回已删除数量。"""
    from services.decode_logs_cleanup import cleanup_expired_decode_logs
    # 第 1 批成功删 500(idx=0),第 2 批抛异常(idx=1)
    db = _FakeDb(rowcounts=[500], raise_on_call=1)
    store = _FakeStore(db)
    result = await cleanup_expired_decode_logs(store, retention_days=7, batch_size=500, max_batches=20)
    # 第 1 批删除 500,第 2 批异常中断
    assert result["deleted_count"] == 500
    # batches_run 在 try 之前 +1,所以第 2 批已计入
    assert result["batches_run"] == 2


@pytest.mark.asyncio
async def test_cleanup_expired_decode_logs_custom_retention():
    """R45: 支持自定义保留天数和批大小。"""
    from services.decode_logs_cleanup import cleanup_expired_decode_logs
    db = _FakeDb(rowcounts=[0])
    store = _FakeStore(db)
    result = await cleanup_expired_decode_logs(store, retention_days=30, batch_size=100, max_batches=5)
    assert result["retention_days"] == 30
    # 验证 SQL 参数中包含 cutoff 和 batch_size
    sql, params = db.execute_calls[0]
    assert "request_time < ?" in sql
    assert "LIMIT ?" in sql
    assert params[2] == 100  # batch_size


@pytest.mark.asyncio
async def test_run_daily_cleanup_loop_calls_cleanup_then_sleeps(monkeypatch):
    """R45: run_daily_cleanup_loop 应调用 cleanup_expired_decode_logs 然后 sleep 86400。"""
    from services import decode_logs_cleanup as mod
    call_count = {"n": 0}

    async def fake_cleanup(store, retention_days=7, **kwargs):
        call_count["n"] += 1
        return {
            "deleted_count": 0,
            "batches_run": 1,
            "retention_days": retention_days,
            "cutoff_time": "",
        }

    monkeypatch.setattr(mod, "cleanup_expired_decode_logs", fake_cleanup)

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # 第一次 sleep(86400)抛 CancelledError 终止循环
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    store = _FakeStore(_FakeDb(rowcounts=[]))
    with pytest.raises(asyncio.CancelledError):
        await mod.run_daily_cleanup_loop(store, retention_days=7)
    # 应调用了一次 cleanup
    assert call_count["n"] == 1
    # 应 sleep 86400(24 小时)
    assert sleep_calls == [86400]


# ─── 3. verify_file_records_status_index 脚本存在测试 ──────────────────────────

def test_verify_file_records_status_index_script_exists():
    """R45: scripts/verify_file_records_status_index.py 应存在。"""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "verify_file_records_status_index.py"
    assert script_path.exists(), f"脚本不存在: {script_path}"


def test_verify_file_records_status_index_script_has_key_functions():
    """R45: 脚本应包含 verify_status_index_usage 函数和 EXPLAIN ANALYZE 逻辑。"""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "verify_file_records_status_index.py"
    content = script_path.read_text(encoding="utf-8")
    assert "async def verify_status_index_usage" in content
    assert "EXPLAIN ANALYZE" in content
    assert "idx_file_records_status" in content
    # 应包含判定建议
    assert "改为部分索引" in content


# ─── 4. r40_scheduler 接入测试 ──────────────────────────

def test_r40_scheduler_has_decode_logs_job():
    """R45: r40_scheduler 应包含 cleanup_expired_decode_logs_job 函数。"""
    from services import r40_scheduler
    assert hasattr(r40_scheduler, "cleanup_expired_decode_logs_job")
    assert callable(r40_scheduler.cleanup_expired_decode_logs_job)


def test_r40_scheduler_references_decode_logs_cleanup():
    """R45: r40_scheduler 源码应引用 run_daily_cleanup_loop 和 create_safe_task。"""
    script_path = Path(__file__).resolve().parent.parent / "services" / "r40_scheduler.py"
    content = script_path.read_text(encoding="utf-8")
    assert "from services.decode_logs_cleanup import run_daily_cleanup_loop" in content
    assert "create_safe_task" in content
    assert "cleanup_expired_decode_logs_job" in content
    # 主循环每天 3:00 调用 decode_logs 清理
    assert "cleanup_expired_decode_logs_job()" in content
