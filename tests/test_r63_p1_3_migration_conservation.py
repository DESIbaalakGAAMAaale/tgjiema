"""R63 P1-03: 004 migration 守恒断言主动降级整改测试。

审计背景(R63 终审报告 P1-03):
  旧 004 migration(`004_effect_receipts_request_hash_unique.sql`)承认将严格等式
  守恒断言降级为 `strict + quarantine <= original`,且 GROUP BY 去重时丢弃的重复行
  未写入独立 evidence 表 — 这无法证明每条原始记录有去向(loser 行可能被静默丢失,
  审计无法回溯)。

  整改措施:
  - 新增 `effect_receipts_r62_duplicates` 取证表,为每条原始 row 记录 source_rowid /
    classification(strict|duplicate|quarantine)/ winner_rowid
  - 去重 loser 行不再静默丢弃,而是在取证表中留痕(classification='duplicate')
  - 守恒断言升级为严格等式:
      count(strict) + count(quarantine)
        + count(duplicates WHERE classification='duplicate') == count(original)
  - 额外证据完整性断言:count(duplicates) == count(original)
  - winner 通过确定性子查询(MAX(created_at) + MAX(rowid) 打破并列)选取,strict INSERT
    与取证分类共享同一 winner 集合,不再用 GROUP BY 静默丢行

测试覆盖矩阵:
  A. 守恒断言(严格等式)— 重复行 + 隔离行 + 保留行混合场景
  B. 证据完整性 — 每条原始 row 在取证表有且仅有一行
  C. 分类正确性 — strict / duplicate / quarantine 计数与去向
  D. winner 选取 — MAX(created_at) + 并列用 MAX(rowid);loser 指向 winner
  E. 边界场景 — 全合法无重复 / 全隔离 / 空表
  F. fail-closed — SQL CHECK 断言不匹配时 ROLLBACK;Python 防御纵深断言
  G. 回归 — 不再用 GROUP BY 静默丢整组(每个合法分组恰有一个 winner 入 strict)

设计说明:
- 测试直接在临时 SQLite 上执行 004 migration 的 DDL + DML,验证守恒/取证行为。
- 使用 sqlite3 原生执行(同步)覆盖 SQL 层;aiosqlite + `_assert_migration_fingerprint`
  覆盖 Python 防御纵深断言。
- 旧表模拟为无 PK 的 drifted schema(允许重复 (a,e,t,rh) 行),匹配 migration 设计
  所针对的"INSERT OR IGNORE 竞态产生数据异常"场景。
- 每个用例独立临时 DB,用例间无状态污染。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_SQL = (
    REPO_ROOT / "database" / "migrations"
    / "004_effect_receipts_request_hash_unique.sql"
)


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

_UNSET = object()  # sentinel:区分 "未传 request_hash"(用默认合法值)与 "显式传 None"(插入 NULL)


def _valid_hash() -> str:
    """返回一个合法的 64 字符请求 hash(内容不限,004 不校验 hex 格式)。"""
    return "r63p103" + "0" * 56  # 64 chars


def _create_legacy_effect_receipts(db: sqlite3.Connection) -> None:
    """创建旧格式 effect_receipts 表(模拟 drifted 旧 schema)。

    旧表无 id 列、无 PK / UNIQUE / CHECK 约束,允许重复 (a,e,t,rh) 行
    (匹配 migration 注释所述"INSERT OR IGNORE 竞态产生数据异常"场景)。
    SQLite 隐式 rowid 用于取证表的 source_rowid / winner_rowid 追踪。
    """
    db.execute("""
        CREATE TABLE effect_receipts (
            action_id       TEXT,
            effect_type     TEXT,
            target          TEXT,
            status          TEXT,
            external_id     TEXT,
            created_at      TEXT,
            completed_at    TEXT,
            request_hash    TEXT,
            attempt         INTEGER,
            lease_owner     TEXT,
            lease_until     TEXT,
            last_error      TEXT,
            reconcile_status TEXT
        )
    """)
    db.commit()


def _run_migration_004(db: sqlite3.Connection) -> None:
    """执行 004 migration SQL(模拟 migrate.py 的 BEGIN IMMEDIATE 包裹)。

    executescript 自动提交当前事务再执行脚本;脚本内多条语句依次执行。
    """
    if not MIGRATION_SQL.exists():
        pytest.skip(f"migration SQL not found: {MIGRATION_SQL}")
    sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
    db.executescript(sql_text)
    db.commit()


def _insert_legacy_row(
    db: sqlite3.Connection,
    *,
    action_id: str,
    effect_type: str = "telegram_send",
    target: str = "t-default",
    status: str = "pending",
    created_at: str = "2026-01-01T00:00:00Z",
    request_hash=_UNSET,
    attempt: int | None = 0,
) -> int:
    """向旧 effect_receipts 表插入一行,返回其 rowid。

    request_hash 未传 → 用默认合法 hash;显式传 None → 插入 NULL;"" → 空串。
    created_at 显式传 None → 插入 NULL(用于构造非法行)。
    """
    if request_hash is _UNSET:
        request_hash = _valid_hash()
    cursor = db.execute(
        "INSERT INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, created_at, "
        " completed_at, request_hash, attempt, lease_owner, lease_until, "
        " last_error, reconcile_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            action_id, effect_type, target, status, None, created_at,
            None, request_hash, attempt, None, None, None, None,
        ),
    )
    db.commit()
    return cursor.lastrowid


@pytest.fixture
def db_with_legacy_data():
    """创建临时 SQLite DB,含旧格式 effect_receipts 表(空)。

    调用方负责 INSERT 旧行后调用 _run_migration_004。
    """
    tmpdir = tempfile.mkdtemp(prefix="r63_p1_3_test_")
    db_path = Path(tmpdir) / "test_migration.db"
    db = sqlite3.connect(str(db_path))
    _create_legacy_effect_receipts(db)
    try:
        yield db
    finally:
        db.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# A. 守恒断言(严格等式)
# ════════════════════════════════════════════════════════════════

class TestConservationStrictEquality:
    """A 组:严格等式 strict + quarantine + duplicates('duplicate') == original。"""

    def test_conservation_with_duplicates_and_quarantine(self, db_with_legacy_data):
        """混合场景:2 唯一合法 + 3 重复(同 a,e,t,rh)+ 2 非法 = 7 原始行。

        期望:strict=3(2 唯一 + 1 winner)、quarantine=2、duplicate=2(loser)。
        守恒:3 + 2 + 2 == 7。
        """
        db = db_with_legacy_data
        rh = _valid_hash()
        # 2 唯一合法行
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh,
                           created_at="2026-01-01T00:00:00Z")
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=rh,
                           created_at="2026-01-02T00:00:00Z")
        # 3 重复行(同 a3/t3/rh,不同 created_at)
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh,
                           created_at="2026-01-03T00:00:00Z", status="pending")
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh,
                           created_at="2026-01-05T00:00:00Z", status="completed")
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh,
                           created_at="2026-01-04T00:00:00Z", status="failed")
        # 2 非法行(空 request_hash)
        _insert_legacy_row(db, action_id="a4", target="t4", request_hash="",
                           created_at="2026-01-06T00:00:00Z")
        _insert_legacy_row(db, action_id="a5", target="t5", request_hash=None,
                           created_at=None)

        original_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0]
        assert original_count == 7

        _run_migration_004(db)

        strict_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0]
        quarantine_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0]
        duplicates_loser_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchone()[0]

        assert strict_count == 3, f"strict 应为 3(2 唯一 + 1 winner),实际 {strict_count}"
        assert quarantine_count == 2, f"quarantine 应为 2,实际 {quarantine_count}"
        assert duplicates_loser_count == 2, (
            f"duplicate loser 应为 2,实际 {duplicates_loser_count}"
        )
        # R63 P1-03 核心断言:严格等式(非 <=)
        assert strict_count + quarantine_count + duplicates_loser_count == original_count, (
            "R63 P1-03 守恒断言失败:strict + quarantine + duplicates != original"
        )

    def test_conservation_no_row_silently_lost(self, db_with_legacy_data):
        """守恒等式保证无行静默丢失 — 旧 `<=` 断言无法捕获的回归。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        # 构造 5 重复行(同组),验证 4 个 loser 全部留痕
        for i in range(5):
            _insert_legacy_row(
                db, action_id="grp", target="tg", request_hash=rh,
                created_at=f"2026-01-0{i+1}T00:00:00Z",
            )
        original_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0]
        assert original_count == 5

        _run_migration_004(db)

        strict_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0]
        quarantine_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0]
        duplicates_loser_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchone()[0]

        assert strict_count == 1, "5 重复行应只保留 1 winner"
        assert duplicates_loser_count == 4, "4 loser 应全部留痕于取证表"
        assert strict_count + quarantine_count + duplicates_loser_count == original_count


# ════════════════════════════════════════════════════════════════
# B. 证据完整性
# ════════════════════════════════════════════════════════════════

class TestEvidenceCompleteness:
    """B 组:duplicates 取证表为每条原始 row 留痕(不论分类)。"""

    def test_every_original_row_has_evidence(self, db_with_legacy_data):
        """取证表行数 == 原始表行数(每条原始 row 有且仅有一条取证记录)。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh)
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=rh)
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=rh,
                           created_at="2026-02-01T00:00:00Z")
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash="")
        _insert_legacy_row(db, action_id="a4", target="t4", request_hash=None,
                           created_at=None)

        original_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone()[0]
        assert original_count == 5

        _run_migration_004(db)

        evidence_count = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates"
        ).fetchone()[0]
        assert evidence_count == original_count, (
            f"证据完整性失败:取证表 {evidence_count} 行 != 原始 {original_count} 行"
        )

    def test_no_duplicate_evidence_rows_per_source(self, db_with_legacy_data):
        """每条原始 row 在取证表中只有一行(无重复取证)。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh)
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh,
                           created_at="2026-02-01T00:00:00Z")
        _run_migration_004(db)

        # 按 source_rowid 分组,每组应只有 1 行
        dup_counts = db.execute(
            "SELECT source_rowid, COUNT(*) FROM effect_receipts_r62_duplicates "
            "GROUP BY source_rowid HAVING COUNT(*) > 1"
        ).fetchall()
        assert dup_counts == [], (
            f"存在 source_rowid 对应多条取证记录: {dup_counts}"
        )


# ════════════════════════════════════════════════════════════════
# C. 分类正确性
# ════════════════════════════════════════════════════════════════

class TestClassificationCorrectness:
    """C 组:strict / duplicate / quarantine 分类计数与去向正确。"""

    def test_classification_counts(self, db_with_legacy_data):
        """strict=唯一+winner / duplicate=loser / quarantine=非法 计数正确。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh)  # strict
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=rh)  # strict
        # 3 重复 → 1 strict + 2 duplicate
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh,
                           created_at="2026-01-03T00:00:00Z")
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh,
                           created_at="2026-01-05T00:00:00Z")
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh,
                           created_at="2026-01-04T00:00:00Z")
        # 2 非法 → 2 quarantine
        _insert_legacy_row(db, action_id="a4", target="t4", request_hash="")
        _insert_legacy_row(db, action_id="a5", target="t5", request_hash=None)

        _run_migration_004(db)

        classifications = dict(db.execute(
            "SELECT classification, COUNT(*) FROM effect_receipts_r62_duplicates "
            "GROUP BY classification"
        ).fetchall())
        assert classifications.get("strict") == 3, (
            f"strict 应为 3(2 唯一 + 1 winner),实际 {classifications.get('strict')}"
        )
        assert classifications.get("duplicate") == 2, (
            f"duplicate 应为 2,实际 {classifications.get('duplicate')}"
        )
        assert classifications.get("quarantine") == 2, (
            f"quarantine 应为 2,实际 {classifications.get('quarantine')}"
        )

    def test_quarantine_rows_have_null_winner(self, db_with_legacy_data):
        """quarantine 分类的取证行 winner_rowid 为 NULL(无 winner)。"""
        db = db_with_legacy_data
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash="")
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=None,
                           created_at=None)
        _run_migration_004(db)

        rows = db.execute(
            "SELECT winner_rowid FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'quarantine'"
        ).fetchall()
        assert len(rows) == 2
        assert all(r[0] is None for r in rows), (
            "quarantine 行 winner_rowid 必须为 NULL"
        )


# ════════════════════════════════════════════════════════════════
# D. winner 选取
# ════════════════════════════════════════════════════════════════

class TestWinnerSelection:
    """D 组:winner = MAX(created_at);并列用 MAX(rowid) 打破。"""

    def test_winner_is_max_created_at(self, db_with_legacy_data):
        """同组多行中,created_at 最新的行成为 winner(入 strict)。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                           created_at="2026-01-01T00:00:00Z", status="pending")
        _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                           created_at="2026-01-05T00:00:00Z", status="completed")
        _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                           created_at="2026-01-03T00:00:00Z", status="failed")
        _run_migration_004(db)

        # strict 表应只有 1 行,且 created_at = 2026-01-05(MAX)
        strict_rows = db.execute(
            "SELECT created_at, status FROM effect_receipts "
            "WHERE action_id = 'a'"
        ).fetchall()
        assert len(strict_rows) == 1
        assert strict_rows[0][0] == "2026-01-05T00:00:00Z", (
            "winner 应为 created_at 最大的行"
        )

    def test_winner_tiebreak_max_rowid(self, db_with_legacy_data):
        """created_at 并列时,用 MAX(rowid) 打破(后插入的行成为 winner)。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        # 三行同 created_at,不同 rowid(递增)
        r1 = _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                                created_at="2026-01-01T00:00:00Z", status="pending")
        r2 = _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                                created_at="2026-01-01T00:00:00Z", status="completed")
        r3 = _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                                created_at="2026-01-01T00:00:00Z", status="failed")
        assert r1 < r2 < r3
        _run_migration_004(db)

        # winner 应为 r3(MAX rowid),取证表中 strict 行 source_rowid == r3
        strict_evidence = db.execute(
            "SELECT source_rowid FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'strict'"
        ).fetchall()
        assert len(strict_evidence) == 1
        assert strict_evidence[0][0] == r3, (
            "并列 created_at 时,winner 应为 MAX(rowid)(确定性)"
        )

    def test_loser_winner_rowid_points_to_actual_winner(self, db_with_legacy_data):
        """loser 行的 winner_rowid 指向同组 winner 的 source_rowid。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                           created_at="2026-01-01T00:00:00Z")
        winner_rowid = _insert_legacy_row(
            db, action_id="a", target="t", request_hash=rh,
            created_at="2026-01-09T00:00:00Z",
        )
        _insert_legacy_row(db, action_id="a", target="t", request_hash=rh,
                           created_at="2026-01-05T00:00:00Z")
        _run_migration_004(db)

        # strict 行的 source_rowid == winner_rowid
        strict_src = db.execute(
            "SELECT source_rowid FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'strict'"
        ).fetchone()[0]
        assert strict_src == winner_rowid

        # 所有 loser 的 winner_rowid == winner_rowid
        loser_winner = db.execute(
            "SELECT winner_rowid FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchall()
        assert len(loser_winner) == 2
        assert all(r[0] == winner_rowid for r in loser_winner), (
            "所有 loser 的 winner_rowid 应指向 winner 的 source_rowid"
        )

    def test_strict_winner_rowid_equals_source_rowid(self, db_with_legacy_data):
        """strict 分类的取证行,winner_rowid == 自身 source_rowid。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh)
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=rh)
        _run_migration_004(db)

        strict_rows = db.execute(
            "SELECT source_rowid, winner_rowid FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'strict'"
        ).fetchall()
        assert len(strict_rows) == 2
        for src, win in strict_rows:
            assert src == win, (
                f"strict 行 winner_rowid({win}) 应等于 source_rowid({src})"
            )


# ════════════════════════════════════════════════════════════════
# E. 边界场景
# ════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """E 组:全合法无重复 / 全隔离 / 空表。"""

    def test_all_unique_legitimate(self, db_with_legacy_data):
        """全部为唯一合法行:strict=original,无 duplicate/quarantine。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash=rh)
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash="b" * 64)
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash="c" * 64)
        original = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        assert original == 3

        _run_migration_004(db)

        strict = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        quarantine = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0]
        dup = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchone()[0]
        assert strict == 3
        assert quarantine == 0
        assert dup == 0
        assert strict + quarantine + dup == original

    def test_all_illegal_quarantined(self, db_with_legacy_data):
        """全部非法行:全部入 quarantine,strict/duplicate=0。"""
        db = db_with_legacy_data
        _insert_legacy_row(db, action_id="a1", target="t1", request_hash="")
        _insert_legacy_row(db, action_id="a2", target="t2", request_hash=None)
        _insert_legacy_row(db, action_id="a3", target="t3", request_hash="",
                           created_at=None)
        original = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        assert original == 3

        _run_migration_004(db)

        strict = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        quarantine = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0]
        dup = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchone()[0]
        assert strict == 0
        assert quarantine == 3
        assert dup == 0
        assert strict + quarantine + dup == original

    def test_empty_table(self, db_with_legacy_data):
        """空表:original=0,守恒等式 0+0+0==0 成立。"""
        db = db_with_legacy_data
        original = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        assert original == 0

        _run_migration_004(db)

        strict = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        quarantine = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0]
        dup = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchone()[0]
        evidence = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates"
        ).fetchone()[0]
        assert strict == 0
        assert quarantine == 0
        assert dup == 0
        assert evidence == 0
        assert strict + quarantine + dup == original


# ════════════════════════════════════════════════════════════════
# F. fail-closed
# ════════════════════════════════════════════════════════════════

class TestFailClosed:
    """F 组:守恒/证据不匹配时 migration 必须 fail-closed。"""

    def test_sql_conservation_assertion_aborts_on_mismatch(self):
        """SQL 守恒断言在 strict+quarantine+duplicates != original 时 raise。

        直接构造不匹配的四张表,执行守恒断言 SQL 片段,验证 CHECK 约束 raise
        IntegrityError(等价 SELECT RAISE(ABORT) 的 fail-closed 效果)。
        """
        tmpdir = tempfile.mkdtemp(prefix="r63_p1_3_failclosed_")
        try:
            db = sqlite3.connect(str(Path(tmpdir) / "fc.db"))
            # 构造不匹配:original=1 但 strict/quarantine/duplicates 都=0
            db.execute("CREATE TABLE effect_receipts_r62_strict (x INTEGER)")
            db.execute("CREATE TABLE effect_receipts_r62_quarantine (x INTEGER)")
            db.execute(
                "CREATE TABLE effect_receipts_r62_duplicates "
                "(classification TEXT)"
            )
            db.execute("CREATE TABLE effect_receipts (x INTEGER)")
            db.execute("INSERT INTO effect_receipts VALUES (1)")
            db.commit()
            assert_sql = """
            CREATE TABLE _r62_conservation_assert (
                is_conserved INTEGER PRIMARY KEY CHECK (is_conserved = 1)
            );
            INSERT INTO _r62_conservation_assert (is_conserved)
            SELECT CASE WHEN
                (SELECT COUNT(*) FROM effect_receipts_r62_strict)
                + (SELECT COUNT(*) FROM effect_receipts_r62_quarantine)
                + (SELECT COUNT(*) FROM effect_receipts_r62_duplicates
                   WHERE classification = 'duplicate')
                = (SELECT COUNT(*) FROM effect_receipts)
                THEN 1 ELSE 0 END;
            """
            with pytest.raises(sqlite3.IntegrityError):
                db.executescript(assert_sql)
            db.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sql_evidence_assertion_aborts_on_missing_evidence(self):
        """SQL 证据完整性断言在取证表行数 != 原始行数时 raise。"""
        tmpdir = tempfile.mkdtemp(prefix="r63_p1_3_evidence_fc_")
        try:
            db = sqlite3.connect(str(Path(tmpdir) / "fc.db"))
            # original=2, duplicates=1(漏记一条)
            db.execute(
                "CREATE TABLE effect_receipts_r62_duplicates (classification TEXT)"
            )
            db.execute("CREATE TABLE effect_receipts (x INTEGER)")
            db.execute("INSERT INTO effect_receipts VALUES (1)")
            db.execute("INSERT INTO effect_receipts VALUES (2)")
            db.execute(
                "INSERT INTO effect_receipts_r62_duplicates VALUES ('strict')"
            )
            db.commit()
            assert_sql = """
            CREATE TABLE _r62_evidence_assert (
                is_complete INTEGER PRIMARY KEY CHECK (is_complete = 1)
            );
            INSERT INTO _r62_evidence_assert (is_complete)
            SELECT CASE WHEN
                (SELECT COUNT(*) FROM effect_receipts_r62_duplicates)
                = (SELECT COUNT(*) FROM effect_receipts)
                THEN 1 ELSE 0 END;
            """
            with pytest.raises(sqlite3.IntegrityError):
                db.executescript(assert_sql)
            db.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# G. 回归 — 不再用 GROUP BY 静默丢整组
# ════════════════════════════════════════════════════════════════

class TestNoSilentGroupLoss:
    """G 组:每个合法 (a,e,t,rh) 分组恰有一个 winner 入 strict(不静默丢整组)。

    回归旧 GROUP BY + HAVING created_at = MAX(created_at) 的潜在缺陷:
    SQLite 中 GROUP BY 的 bare column 取任意行,HAVING 可能误过滤整组,
    导致某些分组在 strict 表中完全消失(旧 `<=` 断言无法捕获)。
    新确定性 winner 选取 + 严格等式断言保证每个合法分组恰有一个 winner。
    """

    def test_every_legitimate_group_has_one_strict_winner(self, db_with_legacy_data):
        """多个合法分组,每个分组恰有一个 winner 入 strict。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        # 4 个不同分组,每组 2-3 重复行
        for grp, n in [("g1", 2), ("g2", 3), ("g3", 2), ("g4", 1)]:
            for i in range(n):
                _insert_legacy_row(
                    db, action_id=grp, target=f"t_{grp}", request_hash=rh,
                    created_at=f"2026-01-0{i+1}T00:00:00Z",
                )
        _run_migration_004(db)

        # 每个分组在 strict 表中应恰有 1 行
        strict_per_group = dict(db.execute(
            "SELECT action_id, COUNT(*) FROM effect_receipts GROUP BY action_id"
        ).fetchall())
        assert set(strict_per_group.keys()) == {"g1", "g2", "g3", "g4"}, (
            f"应有 4 个分组的 winner,实际 {set(strict_per_group.keys())}"
        )
        assert all(c == 1 for c in strict_per_group.values()), (
            f"每个分组应恰有 1 winner,实际 {strict_per_group}"
        )

    def test_no_group_silently_dropped_by_groupby(self, db_with_legacy_data):
        """即使有并列 created_at,分组也不被静默丢弃(确定性 winner 选取)。"""
        db = db_with_legacy_data
        rh = _valid_hash()
        # g1: 3 行全部并列 created_at(旧 GROUP BY+HAVING 可能丢整组的场景)
        for _ in range(3):
            _insert_legacy_row(
                db, action_id="g1", target="t1", request_hash=rh,
                created_at="2026-01-01T00:00:00Z",
            )
        # g2: 2 行,不同 created_at
        _insert_legacy_row(db, action_id="g2", target="t2", request_hash=rh,
                           created_at="2026-01-01T00:00:00Z")
        _insert_legacy_row(db, action_id="g2", target="t2", request_hash=rh,
                           created_at="2026-01-02T00:00:00Z")
        original = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        assert original == 5

        _run_migration_004(db)

        strict = db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        quarantine = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0]
        dup = db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        ).fetchone()[0]
        # g1 → 1 winner(并列用 MAX rowid),g2 → 1 winner → strict=2
        # g1 → 2 loser,g2 → 1 loser → dup=3
        assert strict == 2, f"两个分组应各保留 1 winner,实际 strict={strict}"
        assert dup == 3, f"3 个 loser 应全部留痕,实际 dup={dup}"
        assert strict + quarantine + dup == original


# ════════════════════════════════════════════════════════════════
# H. Python 防御纵深断言(_assert_migration_fingerprint)
# ════════════════════════════════════════════════════════════════

class TestPythonDefenseInDepth:
    """H 组:migrate.py `_assert_migration_fingerprint` 的 004 守恒断言。

    验证 Python 层防御纵深:即使 SQL 层断言被绕过,Python 层跨表 COUNT 比对
    仍能在 COMMIT 前捕获守恒/证据完整性违反(raise RuntimeError → ROLLBACK)。
    """

    @pytest.mark.asyncio
    async def test_python_assertion_passes_after_clean_migration(self):
        """干净 migration 后,Python 守恒断言通过(不 raise)。"""
        import aiosqlite
        try:
            from database.migrate import _assert_migration_fingerprint
        except Exception as e:  # pragma: no cover - 环境降级
            pytest.skip(f"database.migrate 不可导入: {e}")

        tmpdir = tempfile.mkdtemp(prefix="r63_p1_3_py_ok_")
        db_path = Path(tmpdir) / "py.db"
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                await _create_legacy_effect_receipts_async(db)
                rh = _valid_hash()
                await _insert_legacy_row_async(
                    db, action_id="a1", target="t1", request_hash=rh
                )
                await _insert_legacy_row_async(
                    db, action_id="a2", target="t2", request_hash=rh,
                    created_at="2026-02-01T00:00:00Z",
                )
                await _insert_legacy_row_async(
                    db, action_id="a1", target="t1", request_hash=rh,
                    created_at="2026-02-01T00:00:00Z",
                )
                await _insert_legacy_row_async(
                    db, action_id="a3", target="t3", request_hash=""
                )
                sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
                await db.executescript(sql_text)
                await db.commit()
                # 应通过(不 raise)
                await _assert_migration_fingerprint(
                    db, "004_effect_receipts_request_hash_unique.sql"
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_python_assertion_raises_on_evidence_corruption(self):
        """删除取证表行后,Python 证据完整性断言 raise RuntimeError。"""
        import aiosqlite
        try:
            from database.migrate import _assert_migration_fingerprint
        except Exception as e:  # pragma: no cover - 环境降级
            pytest.skip(f"database.migrate 不可导入: {e}")

        tmpdir = tempfile.mkdtemp(prefix="r63_p1_3_py_bad_")
        db_path = Path(tmpdir) / "py.db"
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                await _create_legacy_effect_receipts_async(db)
                rh = _valid_hash()
                await _insert_legacy_row_async(
                    db, action_id="a1", target="t1", request_hash=rh
                )
                await _insert_legacy_row_async(
                    db, action_id="a2", target="t2", request_hash=""
                )
                sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
                await db.executescript(sql_text)
                await db.commit()
                # 破坏:删除一条取证记录(证据完整性违反)
                await db.execute(
                    "DELETE FROM effect_receipts_r62_duplicates "
                    "WHERE source_rowid = ("
                    "  SELECT source_rowid FROM effect_receipts_r62_duplicates "
                    "  LIMIT 1)"
                )
                await db.commit()
                with pytest.raises(RuntimeError, match="evidence completeness"):
                    await _assert_migration_fingerprint(
                        db, "004_effect_receipts_request_hash_unique.sql"
                    )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_python_assertion_raises_on_strict_loss(self):
        """删除 strict 行后,Python 守恒断言 raise RuntimeError(行丢失)。"""
        import aiosqlite
        try:
            from database.migrate import _assert_migration_fingerprint
        except Exception as e:  # pragma: no cover - 环境降级
            pytest.skip(f"database.migrate 不可导入: {e}")

        tmpdir = tempfile.mkdtemp(prefix="r63_p1_3_py_loss_")
        db_path = Path(tmpdir) / "py.db"
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                await _create_legacy_effect_receipts_async(db)
                rh = _valid_hash()
                # 2 唯一合法 + 1 非法
                await _insert_legacy_row_async(
                    db, action_id="a1", target="t1", request_hash=rh
                )
                await _insert_legacy_row_async(
                    db, action_id="a2", target="t2", request_hash=rh
                )
                await _insert_legacy_row_async(
                    db, action_id="a3", target="t3", request_hash=""
                )
                sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
                await db.executescript(sql_text)
                await db.commit()
                # 破坏:从 strict 表删除一行(模拟 winner 丢失)
                await db.execute(
                    "DELETE FROM effect_receipts WHERE id = ("
                    "  SELECT id FROM effect_receipts LIMIT 1)"
                )
                await db.commit()
                with pytest.raises(RuntimeError, match="conservation assertion"):
                    await _assert_migration_fingerprint(
                        db, "004_effect_receipts_request_hash_unique.sql"
                    )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── 异步辅助(aiosqlite 连接,与同步 fixture 解耦)─────────────────

_LEGACY_DDL = """
CREATE TABLE effect_receipts (
    action_id       TEXT,
    effect_type     TEXT,
    target          TEXT,
    status          TEXT,
    external_id     TEXT,
    created_at      TEXT,
    completed_at    TEXT,
    request_hash    TEXT,
    attempt         INTEGER,
    lease_owner     TEXT,
    lease_until     TEXT,
    last_error      TEXT,
    reconcile_status TEXT
)
"""


async def _create_legacy_effect_receipts_async(db) -> None:
    """aiosqlite 连接上创建旧格式 effect_receipts 表。"""
    await db.execute(_LEGACY_DDL)
    await db.commit()


async def _insert_legacy_row_async(
    db,
    *,
    action_id: str,
    effect_type: str = "telegram_send",
    target: str = "t-default",
    status: str = "pending",
    created_at: str = "2026-01-01T00:00:00Z",
    request_hash=_UNSET,
    attempt: int | None = 0,
) -> int:
    """aiosqlite 连接上插入一行旧格式 effect_receipts,返回 rowid。

    request_hash 未传 → 用默认合法 hash;显式传 None → 插入 NULL;"" → 空串。
    """
    if request_hash is _UNSET:
        request_hash = _valid_hash()
    cursor = await db.execute(
        "INSERT INTO effect_receipts "
        "(action_id, effect_type, target, status, external_id, created_at, "
        " completed_at, request_hash, attempt, lease_owner, lease_until, "
        " last_error, reconcile_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            action_id, effect_type, target, status, None, created_at,
            None, request_hash, attempt, None, None, None, None,
        ),
    )
    await db.commit()
    return cursor.lastrowid if cursor.lastrowid is not None else -1
