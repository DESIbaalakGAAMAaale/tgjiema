"""R64 P1-04: outbox 唯一冲突必须按数据库错误类型分类(非字符串匹配)测试。

被测目标:
- ``services.effect_receipts.EffectReceiptManager.record_pending`` —— R64 P1-04 整改:
    * INSERT 失败时的 except 分支不再通过 ``str(err)`` 包含 ``unique``/``constraint``
      子串判断"幂等成功"(反模式:任何 DB 错误消息文本变化都会破坏判断,
      且 CHECK/FK/NOT NULL 的 SQLite 错误消息同样含 "constraint" 字样,
      会被误判为幂等成功)。
    * 新实现按异常类型 + error code / constraint name 精确分类:
      - SQLite: ``sqlite3.IntegrityError`` + ``sqlite_errorcode == SQLITE_CONSTRAINT_UNIQUE``
        (2067) 才视为 UNIQUE 冲突(幂等);其它 IntegrityError(CHECK=275 /
        NOT NULL=1299 / FK=787)必须 raise + 报警。
      - CRDB/PostgreSQL: ``psycopg2.errors.UniqueViolation``(类型检查)。
    * 非 IntegrityError(如 OperationalError: disk full)透传,不被吞掉。

测试覆盖(5 项,与 R64 终审报告 P1-04 要求一一对应):
  1. UNIQUE 约束冲突(SQLITE_CONSTRAINT_UNIQUE=2067)→ 视为幂等成功(不 raise)
  2. CHECK 约束冲突(SQLITE_CONSTRAINT_CHECK=275)→ raise(不视为幂等)
  3. FK 约束冲突(SQLITE_CONSTRAINT_FOREIGNKEY=787)→ raise
  4. NOT NULL 约束冲突(SQLITE_CONSTRAINT_NOTNULL=1299)→ raise
  5. 非 IntegrityError(如 OperationalError)→ 透传(不视为幂等)

测试策略:
- 通过 monkey-patch ``db.execute`` 拦截 INSERT 语句并抛出构造好的 IntegrityError
  (手动设置 ``sqlite_errorcode`` 属性模拟真实 SQLite 抛错行为)。
- PRE-SELECT(无 ``AND request_hash = ?`` 子句)被拦截返回 None,强制走 INSERT 分支。
- SELECT-after-conflict(含 ``AND request_hash = ?``)委托给真实 DB,
  在已预插入"竞态行"的情况下验证 UNIQUE 冲突的幂等路径。
"""
from __future__ import annotations

import inspect
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

# ── SQLite constraint extended error codes(常量,避免 magic number)──
# Python 3.11+ 在 sqlite3 模块暴露这些常量;旧版本回退到硬编码值。
_SQLITE_CONSTRAINT_UNIQUE = getattr(sqlite3, "SQLITE_CONSTRAINT_UNIQUE", 2067)
_SQLITE_CONSTRAINT_CHECK = getattr(sqlite3, "SQLITE_CONSTRAINT_CHECK", 275)
_SQLITE_CONSTRAINT_NOTNULL = getattr(sqlite3, "SQLITE_CONSTRAINT_NOTNULL", 1299)
_SQLITE_CONSTRAINT_FOREIGNKEY = getattr(sqlite3, "SQLITE_CONSTRAINT_FOREIGNKEY", 787)


def _make_sqlite_integrity_error(message: str, error_code: int) -> sqlite3.IntegrityError:
    """构造一个带 ``sqlite_errorcode`` 属性的 ``sqlite3.IntegrityError``。

    真实 SQLite 在抛 IntegrityError 时会设置 ``sqlite_errorcode`` /
    ``sqlite_errorname`` 扩展属性(Python 3.11+ 暴露为公共属性)。
    测试中手动设置这两个属性以模拟真实 DB 抛错行为,从而验证被测代码
    通过 ``getattr(exc, 'sqlite_errorcode', None)`` 精确分类的能力。
    """
    err = sqlite3.IntegrityError(message)
    err.sqlite_errorcode = error_code
    err.sqlite_errorname = {
        _SQLITE_CONSTRAINT_UNIQUE: "SQLITE_CONSTRAINT_UNIQUE",
        _SQLITE_CONSTRAINT_CHECK: "SQLITE_CONSTRAINT_CHECK",
        _SQLITE_CONSTRAINT_NOTNULL: "SQLITE_CONSTRAINT_NOTNULL",
        _SQLITE_CONSTRAINT_FOREIGNKEY: "SQLITE_CONSTRAINT_FOREIGNKEY",
    }.get(error_code, "SQLITE_CONSTRAINT_UNKNOWN")
    return err


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时文件数据库(隔离生产数据)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。

    CacheStore.init() 会创建 effect_receipts 表(R62 P1-01 DDL:
    UNIQUE(a,e,t,rh) + CHECK 约束)。
    """
    tmpdir = tempfile.mkdtemp(prefix="r64_p1_4_test_")
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
# 辅助:预插入一行 effect_receipts(模拟另一 worker 已 INSERT 完成)
# ════════════════════════════════════════════════════════════════

async def _preinsert_receipt(
    store, action_id: str, effect_type: str, target: str,
    request_hash: str, status: str = "pending",
):
    """直接通过 SQL 预插入一行 effect_receipts(模拟另一 worker 已写入)。"""
    now = "2026-07-18T00:00:00"
    await store._db.execute(
        "INSERT INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, "
        " created_at, completed_at, request_hash, attempt, "
        " lease_owner, lease_until, last_error, reconcile_status) "
        "VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, 1, ?, ?, NULL, 'pending')",
        (action_id, effect_type, target, status, now, request_hash, "", ""),
    )
    await store._db.commit()


def _make_insert_intercepting_wrapper(real_db, insert_exc):
    """创建 ``db.execute`` 异步包装器。

    - PRE-SELECT(无 ``AND REQUEST_HASH = ?`` 子句):返回 mock cursor,
      ``fetchone()`` 返回 None,强制 record_pending 走 INSERT 分支
      (跳过 existing 分支,模拟"PRE-SELECT 时行不存在,但 INSERT 时
      另一 worker 已写入"的并发竞态场景)。
    - INSERT INTO effect_receipts:抛出 ``insert_exc``(模拟竞态 INSERT 失败)。
    - 其它 SQL(SELECT-after-conflict / UPDATE / COMMIT 等):委托给真实 DB,
      以验证 UNIQUE 冲突后的 SELECT 兜底能找到预插入的"竞态行"。
    """
    original_execute = real_db.execute

    async def _wrapped(sql, params=None):
        sql_str = sql if isinstance(sql, str) else ""
        sql_upper = sql_str.strip().upper()
        # PRE-SELECT:SELECT status, request_hash FROM effect_receipts
        # WHERE action_id=? AND effect_type=? AND target=?
        # ORDER BY created_at DESC LIMIT 1  (无 request_hash 过滤)
        is_pre_select = (
            sql_upper.startswith("SELECT STATUS, REQUEST_HASH FROM EFFECT_RECEIPTS")
            and "AND REQUEST_HASH = ?" not in sql_upper
        )
        if is_pre_select:
            # 返回空结果,强制走 INSERT 分支
            mock_cursor = MagicMock()
            mock_cursor.fetchone = AsyncMock(return_value=None)
            return mock_cursor
        # INSERT INTO effect_receipts:抛出指定异常(模拟竞态)
        if sql_upper.startswith("INSERT INTO EFFECT_RECEIPTS"):
            raise insert_exc
        # 其它 SQL(SELECT-after-conflict / UPDATE 等)委托给真实 DB
        if params is not None:
            return await original_execute(sql, params)
        return await original_execute(sql)

    return _wrapped


# ════════════════════════════════════════════════════════════════
# 测试:R64 P1-04 UNIQUE 冲突按 DB 错误类型分类
# ════════════════════════════════════════════════════════════════

class TestRecordPendingUniqueConflictClassification:
    """R64 P1-04: record_pending INSERT 失败时按 DB 错误类型精确分类。

    旧实现通过 ``str(err)`` 包含 ``unique``/``constraint`` 子串判断"幂等成功",
    会吞掉 CHECK / NOT NULL / FK 等同样含 "constraint" 字样的 IntegrityError。
    新实现按 ``sqlite_errorcode`` / ``psycopg2.errors.UniqueViolation`` 精确分类:
    仅 UNIQUE 冲突才视为幂等成功,其它 IntegrityError 必须 raise + 报警。
    """

    @pytest.mark.asyncio
    async def test_unique_constraint_conflict_treated_as_idempotent(
        self, real_store, monkeypatch,
    ):
        """UNIQUE 约束冲突(SQLITE_CONSTRAINT_UNIQUE=2067)→ 视为幂等成功,不 raise。

        场景:另一 worker 已 INSERT 同 (a,e,t,rh) 的 pending 行(record_pending
        返回 True,不重复 INSERT,不抛错)。
        """
        from services.effect_receipts import EffectReceiptManager
        mgr = EffectReceiptManager(real_store)
        rh = "rh_uniq_r64" + "0" * 53
        # 预插入一行 pending(模拟另一 worker 已写入,触发 INSERT UNIQUE 冲突)
        await _preinsert_receipt(
            real_store, "act_uniq_r64", "telegram_send", "chat:uniq",
            request_hash=rh, status="pending",
        )
        # 构造真实 UNIQUE IntegrityError(带 sqlite_errorcode=2067)
        unique_err = _make_sqlite_integrity_error(
            "UNIQUE constraint failed: effect_receipts.action_id, "
            "effect_receipts.effect_type, effect_receipts.target, "
            "effect_receipts.request_hash",
            _SQLITE_CONSTRAINT_UNIQUE,
        )
        # 拦截 db.execute:PRE-SELECT 返回 None,INSERT 抛 UNIQUE 异常
        wrapped = _make_insert_intercepting_wrapper(real_store._db, unique_err)
        monkeypatch.setattr(real_store._db, "execute", wrapped)

        # 调用 record_pending:应捕获 UNIQUE 异常并视为幂等成功(返回 True)
        ok = await mgr.record_pending(
            "act_uniq_r64", "telegram_send", "chat:uniq",
            request_hash=rh,
        )
        # 验证幂等成功(不 raise)
        assert ok is True
        # 验证行仍为 pending,attempt 仍为 1(幂等重试不 increment)
        cursor = await real_store._db.execute(
            "SELECT status, attempt FROM effect_receipts "
            "WHERE action_id=? AND effect_type=? AND target=? AND request_hash=?",
            ("act_uniq_r64", "telegram_send", "chat:uniq", rh),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == 1

    @pytest.mark.asyncio
    async def test_check_constraint_conflict_raises_not_idempotent(
        self, real_store, monkeypatch,
    ):
        """CHECK 约束冲突(SQLITE_CONSTRAINT_CHECK=275)→ raise,不视为幂等。

        旧实现因 "constraint" 子串会静默吞掉 CHECK 错误并视为幂等成功;
        新实现通过 sqlite_errorcode 精确分类,CHECK 必须 raise + 报警。
        场景:即使预插入的 pending 行存在(会被 SELECT-after-conflict 找到),
        CHECK 错误也不应被吞掉。
        """
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)
        rh = "rh_check_r64" + "0" * 53
        # 预插入一行 pending(若代码错误地进入 UNIQUE 处理,SELECT 会找到此行,
        # 验证 CHECK 不被误判为幂等:不应返回 True,应 raise)
        await _preinsert_receipt(
            real_store, "act_check_r64", "telegram_send", "chat:check",
            request_hash=rh, status="pending",
        )
        # 构造 CHECK IntegrityError(sqlite_errorcode=275)
        check_err = _make_sqlite_integrity_error(
            "CHECK constraint failed: effect_receipts.status",
            _SQLITE_CONSTRAINT_CHECK,
        )
        wrapped = _make_insert_intercepting_wrapper(real_store._db, check_err)
        monkeypatch.setattr(real_store._db, "execute", wrapped)

        # 调用 record_pending(fail_closed=True):应 raise,不返回 True
        with pytest.raises(EffectReceiptError) as exc_info:
            await mgr.record_pending(
                "act_check_r64", "telegram_send", "chat:check",
                request_hash=rh, fail_closed=True,
            )
        # 验证原始 CHECK IntegrityError 在异常链中(未被静默吞掉)
        cause = exc_info.value.__cause__
        assert cause is not None
        assert isinstance(cause, sqlite3.IntegrityError)
        assert getattr(cause, "sqlite_errorcode", None) == _SQLITE_CONSTRAINT_CHECK
        # 验证错误消息含 "CHECK constraint"(而非 UNIQUE)
        assert "CHECK constraint" in str(cause)

    @pytest.mark.asyncio
    async def test_fk_constraint_conflict_raises_not_idempotent(
        self, real_store, monkeypatch,
    ):
        """FK 约束冲突(SQLITE_CONSTRAINT_FOREIGNKEY=787)→ raise,不视为幂等。"""
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)
        rh = "rh_fk_r64" + "0" * 56
        await _preinsert_receipt(
            real_store, "act_fk_r64", "telegram_send", "chat:fk",
            request_hash=rh, status="pending",
        )
        # 构造 FK IntegrityError(sqlite_errorcode=787)
        fk_err = _make_sqlite_integrity_error(
            "FOREIGN KEY constraint failed: effect_receipts.parent_id",
            _SQLITE_CONSTRAINT_FOREIGNKEY,
        )
        wrapped = _make_insert_intercepting_wrapper(real_store._db, fk_err)
        monkeypatch.setattr(real_store._db, "execute", wrapped)

        with pytest.raises(EffectReceiptError) as exc_info:
            await mgr.record_pending(
                "act_fk_r64", "telegram_send", "chat:fk",
                request_hash=rh, fail_closed=True,
            )
        cause = exc_info.value.__cause__
        assert cause is not None
        assert isinstance(cause, sqlite3.IntegrityError)
        assert getattr(cause, "sqlite_errorcode", None) == _SQLITE_CONSTRAINT_FOREIGNKEY
        assert "FOREIGN KEY constraint" in str(cause)

    @pytest.mark.asyncio
    async def test_not_null_constraint_conflict_raises_not_idempotent(
        self, real_store, monkeypatch,
    ):
        """NOT NULL 约束冲突(SQLITE_CONSTRAINT_NOTNULL=1299)→ raise,不视为幂等。"""
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)
        rh = "rh_nn_r64" + "0" * 55
        await _preinsert_receipt(
            real_store, "act_nn_r64", "telegram_send", "chat:nn",
            request_hash=rh, status="pending",
        )
        # 构造 NOT NULL IntegrityError(sqlite_errorcode=1299)
        nn_err = _make_sqlite_integrity_error(
            "NOT NULL constraint failed: effect_receipts.request_hash",
            _SQLITE_CONSTRAINT_NOTNULL,
        )
        wrapped = _make_insert_intercepting_wrapper(real_store._db, nn_err)
        monkeypatch.setattr(real_store._db, "execute", wrapped)

        with pytest.raises(EffectReceiptError) as exc_info:
            await mgr.record_pending(
                "act_nn_r64", "telegram_send", "chat:nn",
                request_hash=rh, fail_closed=True,
            )
        cause = exc_info.value.__cause__
        assert cause is not None
        assert isinstance(cause, sqlite3.IntegrityError)
        assert getattr(cause, "sqlite_errorcode", None) == _SQLITE_CONSTRAINT_NOTNULL
        assert "NOT NULL constraint" in str(cause)

    @pytest.mark.asyncio
    async def test_non_integrity_error_passthrough(
        self, real_store, monkeypatch,
    ):
        """非 IntegrityError(如 OperationalError: disk full)→ 透传,不视为幂等。

        OperationalError 既不是 sqlite3.IntegrityError,也没有 "unique"/"constraint"
        子串,任何实现都应透传。此测试验证新实现不会因异常类型检查而误吞
        非 IntegrityError 异常。
        """
        from services.effect_receipts import EffectReceiptManager, EffectReceiptError
        mgr = EffectReceiptManager(real_store)
        rh = "rh_oe_r64" + "0" * 55
        # 构造 OperationalError(disk I/O error,非 IntegrityError)
        op_err = sqlite3.OperationalError("disk I/O error")
        wrapped = _make_insert_intercepting_wrapper(real_store._db, op_err)
        monkeypatch.setattr(real_store._db, "execute", wrapped)

        with pytest.raises(EffectReceiptError) as exc_info:
            await mgr.record_pending(
                "act_oe_r64", "telegram_send", "chat:oe",
                request_hash=rh, fail_closed=True,
            )
        # 验证原始 OperationalError 在异常链中(未被静默吞掉)
        cause = exc_info.value.__cause__
        assert cause is not None
        assert isinstance(cause, sqlite3.OperationalError)
        assert not isinstance(cause, sqlite3.IntegrityError)  # 非 IntegrityError
        assert "disk I/O error" in str(cause)
