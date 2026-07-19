"""R64 P1-03: migration 004 必须用实际 SQL 证明严格守恒。

R64 终审报告 P1-03 整改要求:
  - 对 original、strict、quarantine、duplicates 四组使用稳定 row identity;
    断言每个原始 row_id 恰好出现一次。
  - original_count = strict_count + quarantine_count + duplicate_evidence_count
  - 保存原始 payload hash、冲突组、保留行、淘汰原因;迁移回滚/重跑必须幂等。

背景:
  R63 已新增 effect_receipts_r62_duplicates 取证表 + 严格等式断言,manifest 的
  r63_p1_03_note 字段宣称"duplicates evidence 与严格等式已实现"。但 R64 终审认为
  "说明文字不是证明",要求用实际 SQL 执行后对四组(row identity 维度)做断言。

  本测试直接执行 004 migration 的实际 SQL(database/migrations/
  004_effect_receipts_request_hash_unique.sql),非模拟守恒逻辑;然后对四组结果
  做 row-identity 级别的断言。四组定义(rename 后):
    - original:    effect_receipts_invalid_r62(旧表 rename 后,rowid 稳定保留)
    - strict:      effect_receipts(新表 rename 后)— winner 行,UNIQUE(a,e,t,rh)
    - quarantine:  effect_receipts_r62_quarantine — 非法行隔离
    - duplicates:  effect_receipts_r62_duplicates — 每条原始 row 的取证记录
                   (classification: strict|duplicate|quarantine)

  注:测试验证的是守恒等式和 row identity 唯一性,具体表结构依据 migration 004 的
  实际 DDL(effect_receipts_r62_strict → rename effect_receipts;
  effect_receipts_r62_quarantine;effect_receipts_r62_duplicates;临时表
  _r62_winner_rowids 在 rename 前 DROP)。

核心断言矩阵:
  A. 严格守恒等式 — original == strict + quarantine + duplicate_evidence_count
  B. 稳定 row identity — 每个原始 rowid 在 duplicates 恰好出现一次(无重复取证)
  C. row identity 完整性 — duplicates.source_rowid 集合 == original.rowid 集合
  D. 分类一致性 — strict/duplicate/quarantine 三类 source_rowid 两两不相交,
                  并集 == original.rowid 集合(每个原始 rowid 恰好落入一类)
  E. payload hash 保存 — duplicates.request_hash 保留原始 payload hash
  F. 冲突组保存 — duplicates 保留 (action_id, effect_type, target, request_hash)
  G. 保留行 — strict 分类 winner_rowid == source_rowid;loser winner_rowid 指向 winner
  H. 淘汰原因 — classification 字段 + quarantine.quarantine_reason 字段
  I. 幂等性 — 相同输入两次独立 migration 产生相同结果(确定性 winner 选取)
  J. 回滚安全 — strict ∪ quarantine ∪ duplicate_loser 的 source_rowid 集合
                == original.rowid 集合(无信息丢失,可重建 original);
                且事务 ROLLBACK 后 DB 回到 pre-migration 状态
  K. fail-closed — 守恒等式违反时 SQL CHECK 约束 raise(事务 ROLLBACK)

  使用 sqlite3 内存数据库模拟(不依赖真实 CRDB),直接 executescript migration 004 SQL。
  旧表模拟为无 PK 的 drifted schema(允许重复 (a,e,t,rh) 行),匹配 migration 设计
  所针对的"INSERT OR IGNORE 竞态产生数据异常"场景。
"""
from __future__ import annotations

import sqlite3
import sys
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

_UNSET = object()  # sentinel:区分 "未传 request_hash"(用默认合法值)与 "显式传 None"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

def _valid_hash(seed: str = "r64p103") -> str:
    """返回一个合法的 64 字符请求 hash(004 不校验 hex 格式,只校验非空)。"""
    return (seed + "0" * 64)[:64]


def _create_legacy_effect_receipts(db: sqlite3.Connection) -> None:
    """创建旧格式 effect_receipts 表(模拟 drifted 旧 schema,无 PK / UNIQUE / CHECK)。

    旧表允许重复 (a,e,t,rh) 行,匹配 migration 注释所述"INSERT OR IGNORE 竞态产生
    数据异常"场景。SQLite 隐式 rowid 作为稳定 row identity,供取证表追踪。
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
    """执行 004 migration 实际 SQL(模拟 migrate.py 的 BEGIN IMMEDIATE 包裹)。

    executescript 自动提交当前事务再执行脚本;脚本内多条语句依次执行。
    任一 CHECK 断言失败 → IntegrityError(等价 fail-closed ROLLBACK)。
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
    created_at: str | None = "2026-01-01T00:00:00Z",
    request_hash=_UNSET,
    attempt: int | None = 0,
) -> int:
    """向旧 effect_receipts 表插入一行,返回其 rowid(稳定 row identity)。

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


def _seed_mixed_data(db: sqlite3.Connection) -> dict:
    """向旧 effect_receipts 表插入混合数据(唯一合法 + 重复 + 非法 + 并列)。

    返回 metadata 字典,记录每行的 rowid 与预期分类,供断言使用。

    数据布局(共 9 原始行):
      - 2 唯一合法行(a1/t1, a2/t2)— 预期 strict
      - 3 重复行(a3/t3 同 rh,不同 created_at)— 1 strict(winner) + 2 duplicate
      - 2 非法行(a4 空 rh, a5 NULL rh + NULL created_at)— 2 quarantine
      - 2 重复行(a6/t6 同 rh,并列 created_at)— 1 strict(MAX rowid) + 1 duplicate
    预期 migration 后:
      strict_count = 4, quarantine_count = 2, duplicate_loser_count = 3
      original_count = 9;守恒:4 + 2 + 3 == 9
    """
    rh_a3 = _valid_hash("a3grp")
    rh_a6 = _valid_hash("a6grp")
    meta: dict = {"rows": [], "groups": {}}

    r1 = _insert_legacy_row(db, action_id="a1", target="t1",
                            request_hash=_valid_hash("a1"),
                            created_at="2026-01-01T00:00:00Z")
    meta["rows"].append({"rowid": r1, "expect": "strict", "group": "a1"})

    r2 = _insert_legacy_row(db, action_id="a2", target="t2",
                            request_hash=_valid_hash("a2"),
                            created_at="2026-01-02T00:00:00Z")
    meta["rows"].append({"rowid": r2, "expect": "strict", "group": "a2"})

    # a3 group: 3 rows, winner = MAX created_at = 2026-01-05 (r3b)
    r3a = _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh_a3,
                             created_at="2026-01-03T00:00:00Z", status="pending")
    r3b = _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh_a3,
                             created_at="2026-01-05T00:00:00Z", status="completed")
    r3c = _insert_legacy_row(db, action_id="a3", target="t3", request_hash=rh_a3,
                             created_at="2026-01-04T00:00:00Z", status="failed")
    meta["rows"].append({"rowid": r3a, "expect": "duplicate", "group": "a3"})
    meta["rows"].append({"rowid": r3b, "expect": "strict", "group": "a3"})
    meta["rows"].append({"rowid": r3c, "expect": "duplicate", "group": "a3"})
    meta["groups"]["a3"] = {"winner": r3b, "losers": [r3a, r3c], "rh": rh_a3}

    # 2 illegal rows
    r4 = _insert_legacy_row(db, action_id="a4", target="t4", request_hash="",
                            created_at="2026-01-06T00:00:00Z")
    r5 = _insert_legacy_row(db, action_id="a5", target="t5", request_hash=None,
                            created_at=None)
    meta["rows"].append({"rowid": r4, "expect": "quarantine", "group": "a4"})
    meta["rows"].append({"rowid": r5, "expect": "quarantine", "group": "a5"})

    # a6 group: 2 rows, tied created_at, winner = MAX rowid (r6b)
    r6a = _insert_legacy_row(db, action_id="a6", target="t6", request_hash=rh_a6,
                             created_at="2026-01-07T00:00:00Z", status="pending")
    r6b = _insert_legacy_row(db, action_id="a6", target="t6", request_hash=rh_a6,
                             created_at="2026-01-07T00:00:00Z", status="completed")
    meta["rows"].append({"rowid": r6a, "expect": "duplicate", "group": "a6"})
    meta["rows"].append({"rowid": r6b, "expect": "strict", "group": "a6"})
    meta["groups"]["a6"] = {"winner": r6b, "losers": [r6a], "rh": rh_a6}

    return meta


def _build_migrated_mixed_db() -> tuple[sqlite3.Connection, dict]:
    """创建内存 DB,seed 混合数据,执行 migration 004,返回 (db, metadata)。"""
    db = sqlite3.connect(":memory:")
    _create_legacy_effect_receipts(db)
    meta = _seed_mixed_data(db)
    _run_migration_004(db)
    return db, meta


# ════════════════════════════════════════════════════════════════
# A. 严格守恒等式
# ════════════════════════════════════════════════════════════════

class TestStrictConservationEquation:
    """A 组:实际 SQL 执行后,四组 COUNT 满足严格等式。

    original_count = strict_count + quarantine_count + duplicate_evidence_count

    其中 duplicate_evidence_count = COUNT(duplicates WHERE classification='duplicate')
    (loser 行,仅取证不入 strict/quarantine)。
    """

    def test_conservation_equation_holds(self):
        """混合场景下严格等式成立:4 + 2 + 3 == 9。"""
        db, _ = _build_migrated_mixed_db()
        try:
            original_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
            ).fetchone()[0]
            strict_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts"
            ).fetchone()[0]
            quarantine_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
            ).fetchone()[0]
            dup_evidence_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'duplicate'"
            ).fetchone()[0]

            assert original_count == 9, f"original 应为 9,实际 {original_count}"
            assert strict_count == 4, f"strict 应为 4,实际 {strict_count}"
            assert quarantine_count == 2, f"quarantine 应为 2,实际 {quarantine_count}"
            assert dup_evidence_count == 3, (
                f"duplicate evidence 应为 3,实际 {dup_evidence_count}"
            )
            # R64 P1-03 核心断言:严格等式(非 <=)
            assert original_count == strict_count + quarantine_count + dup_evidence_count, (
                f"守恒等式失败: {original_count} != "
                f"{strict_count} + {quarantine_count} + {dup_evidence_count}"
            )
        finally:
            db.close()

    def test_no_row_lost_no_row_duplicated(self):
        """严格等式保证无行丢失、无行重复(== 而非 <=)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            original = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
            ).fetchone()[0]
            strict = db.execute(
                "SELECT COUNT(*) FROM effect_receipts"
            ).fetchone()[0]
            quarantine = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
            ).fetchone()[0]
            dup = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'duplicate'"
            ).fetchone()[0]
            # 严格等式(不是 <=)— 旧 R62 实现是 <=,无法捕获 loser 丢失
            assert strict + quarantine + dup == original, (
                f"无行丢失/重复失败: {strict}+{quarantine}+{dup} != {original}"
            )
        finally:
            db.close()

    def test_edge_cases_preserve_equation(self):
        """边界场景(空表 / 全合法 / 全非法)守恒等式均成立。"""
        # 空表
        db = sqlite3.connect(":memory:")
        _create_legacy_effect_receipts(db)
        _run_migration_004(db)
        assert db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification='duplicate'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
        ).fetchone()[0] == 0
        db.close()

        # 全合法无重复
        db = sqlite3.connect(":memory:")
        _create_legacy_effect_receipts(db)
        _insert_legacy_row(db, action_id="x1", target="t1",
                           request_hash=_valid_hash("x1"))
        _insert_legacy_row(db, action_id="x2", target="t2",
                           request_hash=_valid_hash("x2"))
        _run_migration_004(db)
        assert db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification='duplicate'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
        ).fetchone()[0] == 2
        db.close()

        # 全非法
        db = sqlite3.connect(":memory:")
        _create_legacy_effect_receipts(db)
        _insert_legacy_row(db, action_id="y1", target="t1", request_hash="")
        _insert_legacy_row(db, action_id="y2", target="t2", request_hash=None,
                           created_at=None)
        _run_migration_004(db)
        assert db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification='duplicate'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
        ).fetchone()[0] == 2
        db.close()


# ════════════════════════════════════════════════════════════════
# B/C/D. 稳定 row identity
# ════════════════════════════════════════════════════════════════

class TestStableRowIdentity:
    """B/C/D 组:稳定 row identity — 每个原始 rowid 恰好出现一次。

    断言:
      B. 每个原始 rowid 在 duplicates 表中恰好出现一次(无重复取证)
      C. duplicates.source_rowid 集合 == original.rowid 集合(无遗漏)
      D. strict/duplicate/quarantine 三类 source_rowid 两两不相交,
         并集 == original.rowid 集合(每个原始 rowid 恰好落入一类)
    """

    def test_every_original_rowid_appears_exactly_once_in_evidence(self):
        """B:每个原始 rowid 在 duplicates 表中恰好出现一次(无重复取证)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            dup_counts = db.execute(
                "SELECT source_rowid, COUNT(*) AS c "
                "FROM effect_receipts_r62_duplicates "
                "WHERE source_rowid IS NOT NULL "
                "GROUP BY source_rowid HAVING c > 1"
            ).fetchall()
            assert dup_counts == [], (
                f"存在 source_rowid 在取证表出现多次: {dup_counts}"
            )
        finally:
            db.close()

    def test_evidence_source_rowids_equal_original_rowids(self):
        """C:duplicates.source_rowid 集合 == original.rowid 集合(无遗漏)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            orig_rowids = set(r[0] for r in db.execute(
                "SELECT rowid FROM effect_receipts_invalid_r62"
            ).fetchall())
            evidence_src = set(r[0] for r in db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE source_rowid IS NOT NULL"
            ).fetchall())
            assert evidence_src == orig_rowids, (
                f"取证表 source_rowid 集合 != 原始 rowid 集合\n"
                f"missing from evidence: {orig_rowids - evidence_src}\n"
                f"extra in evidence: {evidence_src - orig_rowids}"
            )
        finally:
            db.close()

    def test_classification_partition_is_disjoint_and_complete(self):
        """D:三类 source_rowid 两两不相交,并集 == original.rowid 集合。

        每个原始 rowid 恰好落入 strict / quarantine / duplicate 三类之一。
        """
        db, _ = _build_migrated_mixed_db()
        try:
            orig = set(r[0] for r in db.execute(
                "SELECT rowid FROM effect_receipts_invalid_r62"
            ).fetchall())
            strict_src = set(r[0] for r in db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'strict'"
            ).fetchall())
            quarantine_src = set(r[0] for r in db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'quarantine'"
            ).fetchall())
            dup_src = set(r[0] for r in db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'duplicate'"
            ).fetchall())

            # 两两不相交
            assert strict_src & quarantine_src == set(), (
                f"strict ∩ quarantine 非空: {strict_src & quarantine_src}"
            )
            assert strict_src & dup_src == set(), (
                f"strict ∩ duplicate 非空: {strict_src & dup_src}"
            )
            assert quarantine_src & dup_src == set(), (
                f"quarantine ∩ duplicate 非空: {quarantine_src & dup_src}"
            )
            # 并集 == original
            union = strict_src | quarantine_src | dup_src
            assert union == orig, (
                f"三类并集 != original\n"
                f"missing: {orig - union}\n"
                f"extra: {union - orig}"
            )
        finally:
            db.close()

    def test_each_original_rowid_has_expected_classification(self):
        """每个原始 rowid 的实际分类与预期一致(metadata 驱动)。"""
        db, meta = _build_migrated_mixed_db()
        try:
            actual = dict(db.execute(
                "SELECT source_rowid, classification "
                "FROM effect_receipts_r62_duplicates"
            ).fetchall())
            assert len(actual) == 9, f"取证表应有 9 行,实际 {len(actual)}"
            for row in meta["rows"]:
                rid = row["rowid"]
                expected = row["expect"]
                assert rid in actual, f"rowid {rid} 未在取证表"
                assert actual[rid] == expected, (
                    f"rowid {rid} 分类应为 {expected},实际 {actual[rid]}"
                )
        finally:
            db.close()

    def test_strict_rowids_map_to_strict_table_one_to_one(self):
        """strict 分类的 source_rowid 数量 == strict 表行数(一一对应)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            strict_evidence_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'strict'"
            ).fetchone()[0]
            strict_table_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts"
            ).fetchone()[0]
            assert strict_evidence_count == strict_table_count == 4, (
                f"strict evidence({strict_evidence_count}) != "
                f"strict table({strict_table_count})"
            )
        finally:
            db.close()

    def test_quarantine_rowids_map_to_quarantine_table_one_to_one(self):
        """quarantine 分类的 source_rowid 数量 == quarantine 表行数。"""
        db, _ = _build_migrated_mixed_db()
        try:
            q_evidence = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'quarantine'"
            ).fetchone()[0]
            q_table = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
            ).fetchone()[0]
            assert q_evidence == q_table == 2, (
                f"quarantine evidence({q_evidence}) != quarantine table({q_table})"
            )
        finally:
            db.close()


# ════════════════════════════════════════════════════════════════
# E/F/G/H. payload hash / 冲突组 / 保留行 / 淘汰原因
# ════════════════════════════════════════════════════════════════

class TestEvidenceFieldsPreserved:
    """E/F/G/H 组:保存原始 payload hash、冲突组、保留行、淘汰原因。"""

    def test_payload_hash_preserved_in_evidence(self):
        """E:duplicates.request_hash == original.request_hash(原始 payload hash 保存)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            # SQL 中 NULL IS NULL 用 IS 处理;此处用 IFNULL 兜底比较
            mismatches = db.execute(
                "SELECT d.source_rowid, d.request_hash AS ev_hash, "
                "       o.request_hash AS orig_hash "
                "FROM effect_receipts_r62_duplicates d "
                "JOIN effect_receipts_invalid_r62 o ON o.rowid = d.source_rowid "
                "WHERE IFNULL(d.request_hash,'<null>') "
                "      != IFNULL(o.request_hash,'<null>')"
            ).fetchall()
            assert mismatches == [], (
                f"取证表 request_hash 与原始不匹配: {mismatches}"
            )
        finally:
            db.close()

    def test_conflict_group_fields_preserved(self):
        """F:duplicates 保留 (action_id, effect_type, target, request_hash) 冲突组。"""
        db, _ = _build_migrated_mixed_db()
        try:
            mismatches = db.execute(
                "SELECT d.source_rowid "
                "FROM effect_receipts_r62_duplicates d "
                "JOIN effect_receipts_invalid_r62 o ON o.rowid = d.source_rowid "
                "WHERE IFNULL(d.action_id,'') != IFNULL(o.action_id,'') "
                "   OR IFNULL(d.effect_type,'') != IFNULL(o.effect_type,'') "
                "   OR IFNULL(d.target,'') != IFNULL(o.target,'') "
                "   OR IFNULL(d.request_hash,'') != IFNULL(o.request_hash,'')"
            ).fetchall()
            assert mismatches == [], (
                f"取证表冲突组字段与原始不匹配: {mismatches}"
            )
        finally:
            db.close()

    def test_strict_winner_self_reference(self):
        """G:strict 分类 winner_rowid == source_rowid(winner 自指)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            bad = db.execute(
                "SELECT source_rowid, winner_rowid "
                "FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'strict' "
                "  AND winner_rowid != source_rowid"
            ).fetchall()
            assert bad == [], (
                f"strict 行 winner_rowid 应 == source_rowid: {bad}"
            )
        finally:
            db.close()

    def test_quarantine_winner_rowid_is_null(self):
        """G:quarantine 分类 winner_rowid 为 NULL(无 winner)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            bad = db.execute(
                "SELECT source_rowid, winner_rowid "
                "FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'quarantine' "
                "  AND winner_rowid IS NOT NULL"
            ).fetchall()
            assert bad == [], (
                f"quarantine 行 winner_rowid 应为 NULL: {bad}"
            )
        finally:
            db.close()

    def test_loser_winner_rowid_points_to_group_winner(self):
        """G:loser(duplicate)的 winner_rowid 指向同组 winner 的 source_rowid。"""
        db, meta = _build_migrated_mixed_db()
        try:
            for grp_name, grp in meta["groups"].items():
                winner_rid = grp["winner"]
                loser_rids = grp["losers"]
                for loser_rid in loser_rids:
                    row = db.execute(
                        "SELECT winner_rowid FROM effect_receipts_r62_duplicates "
                        "WHERE source_rowid = ? AND classification = 'duplicate'",
                        (loser_rid,),
                    ).fetchone()
                    assert row is not None, (
                        f"loser rowid {loser_rid}({grp_name}) 未在取证表"
                    )
                    assert row[0] == winner_rid, (
                        f"loser {loser_rid}({grp_name}) winner_rowid 应为 "
                        f"{winner_rid},实际 {row[0]}"
                    )
        finally:
            db.close()

    def test_strict_winner_data_migrated_to_strict_table(self):
        """G:strict 分类行的数据已迁移到 strict 表(按 a,e,t,rh,created_at 匹配)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            strict_evidence = db.execute(
                "SELECT d.source_rowid, d.action_id, d.effect_type, "
                "       d.target, d.request_hash, o.created_at "
                "FROM effect_receipts_r62_duplicates d "
                "JOIN effect_receipts_invalid_r62 o ON o.rowid = d.source_rowid "
                "WHERE d.classification = 'strict'"
            ).fetchall()
            assert len(strict_evidence) == 4
            for src, aid, etype, tgt, rh, created in strict_evidence:
                match = db.execute(
                    "SELECT COUNT(*) FROM effect_receipts "
                    "WHERE action_id = ? AND effect_type = ? AND target = ? "
                    "  AND request_hash = ? AND created_at = ?",
                    (aid, etype, tgt, rh, created),
                ).fetchone()[0]
                assert match == 1, (
                    f"strict 分类 rowid {src} 的数据未迁移到 strict 表 "
                    f"(a={aid}, e={etype}, t={tgt}, rh={rh}, created={created}), "
                    f"匹配 {match} 行"
                )
        finally:
            db.close()

    def test_quarantine_reason_recorded(self):
        """H:quarantine 表记录淘汰原因(quarantine_reason 字段非空)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            reasons = db.execute(
                "SELECT quarantine_reason FROM effect_receipts_r62_quarantine "
                "ORDER BY quarantine_reason"
            ).fetchall()
            assert len(reasons) == 2
            reason_vals = [r[0] for r in reasons]
            # a4: 空 request_hash → empty_request_hash
            # a5: NULL request_hash → empty_request_hash(CASE 第一个分支匹配)
            valid_reasons = {
                "empty_request_hash", "empty_action_id", "empty_effect_type",
                "empty_target", "empty_created_at", "unknown",
            }
            assert all(r in valid_reasons for r in reason_vals), (
                f"非法 reason 值: {reason_vals}"
            )
            assert "empty_request_hash" in reason_vals, (
                f"应至少有一个 empty_request_hash: {reason_vals}"
            )
        finally:
            db.close()

    def test_evidence_classification_is_elimination_reason(self):
        """H:duplicates.classification 字段标识淘汰原因(strict/duplicate/quarantine)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            classifications = dict(db.execute(
                "SELECT classification, COUNT(*) FROM effect_receipts_r62_duplicates "
                "GROUP BY classification"
            ).fetchall())
            assert classifications.get("strict") == 4, (
                f"strict 应为 4,实际 {classifications.get('strict')}"
            )
            assert classifications.get("duplicate") == 3, (
                f"duplicate 应为 3,实际 {classifications.get('duplicate')}"
            )
            assert classifications.get("quarantine") == 2, (
                f"quarantine 应为 2,实际 {classifications.get('quarantine')}"
            )
            assert sum(classifications.values()) == 9
        finally:
            db.close()

    def test_migrated_at_timestamp_recorded(self):
        """H:duplicates.migrated_at 记录迁移时间(非空,可追溯)。"""
        db, _ = _build_migrated_mixed_db()
        try:
            null_count = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                "WHERE migrated_at IS NULL OR migrated_at = ''"
            ).fetchone()[0]
            assert null_count == 0, f"{null_count} 条取证记录 migrated_at 为空"
        finally:
            db.close()


# ════════════════════════════════════════════════════════════════
# I. 幂等性 — 确定性 winner 选取
# ════════════════════════════════════════════════════════════════

class TestDeterministicIdempotentRerun:
    """I 组:相同输入两次独立 migration 产生相同结果(幂等性 / 确定性)。

    migration 004 的 winner 选取是确定性的(MAX(created_at) + MAX(rowid) 并列打破),
    因此相同输入应产生相同的 strict/quarantine/duplicates 结果。这保证迁移回滚后
    重跑能得到一致结果(无副作用、无歧义)。
    """

    def test_two_independent_runs_produce_identical_counts(self):
        """两次独立 migration 产生相同的 strict/quarantine/duplicates/evidence 计数。"""
        results = []
        for _ in range(2):
            db = sqlite3.connect(":memory:")
            _create_legacy_effect_receipts(db)
            _seed_mixed_data(db)
            _run_migration_004(db)
            counts = (
                db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                    "WHERE classification='duplicate'"
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM effect_receipts_r62_duplicates"
                ).fetchone()[0],
                db.execute(
                    "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
                ).fetchone()[0],
            )
            results.append(counts)
            db.close()
        assert results[0] == results[1], (
            f"两次独立 migration 计数不一致: {results[0]} vs {results[1]}"
        )

    def test_two_independent_runs_produce_identical_classification_mapping(self):
        """两次独立 migration 产生相同的 source_rowid → classification 映射。"""
        mappings = []
        for _ in range(2):
            db = sqlite3.connect(":memory:")
            _create_legacy_effect_receipts(db)
            _seed_mixed_data(db)
            _run_migration_004(db)
            mapping = dict(db.execute(
                "SELECT source_rowid, classification "
                "FROM effect_receipts_r62_duplicates"
            ).fetchall())
            mappings.append(mapping)
            db.close()
        assert mappings[0] == mappings[1], (
            "两次 migration 的 source_rowid → classification 映射不一致"
        )

    def test_two_independent_runs_produce_identical_winner_mapping(self):
        """两次独立 migration 产生相同的 source_rowid → winner_rowid 映射。"""
        mappings = []
        for _ in range(2):
            db = sqlite3.connect(":memory:")
            _create_legacy_effect_receipts(db)
            _seed_mixed_data(db)
            _run_migration_004(db)
            mapping = dict(db.execute(
                "SELECT source_rowid, winner_rowid "
                "FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'duplicate'"
            ).fetchall())
            mappings.append(mapping)
            db.close()
        assert mappings[0] == mappings[1], (
            "两次 migration 的 loser → winner 映射不一致"
        )

    def test_deterministic_winner_under_tied_created_at(self):
        """并列 created_at 时,winner = MAX(rowid)(确定性,可复现)。"""
        expected_winner = None
        winners = []
        for _ in range(3):
            db = sqlite3.connect(":memory:")
            _create_legacy_effect_receipts(db)
            meta = _seed_mixed_data(db)
            _run_migration_004(db)
            w = db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'strict' "
                "  AND action_id = 'a6' AND target = 't6'"
            ).fetchone()[0]
            winners.append(w)
            if expected_winner is None:
                expected_winner = meta["groups"]["a6"]["winner"]
            db.close()
        assert len(set(winners)) == 1, f"winner 不确定: {winners}"
        assert winners[0] == expected_winner, (
            f"winner 应为 {expected_winner},实际 {winners[0]}"
        )

    def test_deterministic_winner_under_distinct_created_at(self):
        """不同 created_at 时,winner = MAX(created_at)(确定性,可复现)。"""
        expected_winner = None
        winners = []
        for _ in range(3):
            db = sqlite3.connect(":memory:")
            _create_legacy_effect_receipts(db)
            meta = _seed_mixed_data(db)
            _run_migration_004(db)
            w = db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE classification = 'strict' "
                "  AND action_id = 'a3' AND target = 't3'"
            ).fetchone()[0]
            winners.append(w)
            if expected_winner is None:
                expected_winner = meta["groups"]["a3"]["winner"]
            db.close()
        assert len(set(winners)) == 1, f"winner 不确定: {winners}"
        assert winners[0] == expected_winner, (
            f"winner 应为 {expected_winner},实际 {winners[0]}"
        )


# ════════════════════════════════════════════════════════════════
# J. 回滚安全 — 可重建 original
# ════════════════════════════════════════════════════════════════

class TestRollbackSafety:
    """J 组:从 strict + quarantine + duplicate_evidence 可重建 original(无信息丢失)。

    回滚安全:
      - strict(winner) + quarantine(非法) + duplicate(loser) 三组的 source_rowid
        集合 == original.rowid 集合(无信息丢失,可重建 original)
      - original 表(rename 后 effect_receipts_invalid_r62)保留所有原始数据
      - 事务 ROLLBACK 后 DB 回到 pre-migration 状态(SQLite 事务性 DDL)
    """

    def test_original_table_preserved_not_dropped(self):
        """original 表(rename 后)保留所有原始数据,未被丢弃。"""
        db, _ = _build_migrated_mixed_db()
        try:
            cnt = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
            ).fetchone()[0]
            assert cnt == 9, f"original 表应保留 9 行,实际 {cnt}"
            rowids = set(r[0] for r in db.execute(
                "SELECT rowid FROM effect_receipts_invalid_r62"
            ).fetchall())
            assert len(rowids) == 9
        finally:
            db.close()

    def test_original_rowids_reconstructable_from_three_groups(self):
        """strict ∪ quarantine ∪ duplicate 的 source_rowid == original.rowid 集合。"""
        db, _ = _build_migrated_mixed_db()
        try:
            orig = set(r[0] for r in db.execute(
                "SELECT rowid FROM effect_receipts_invalid_r62"
            ).fetchall())
            three_group = set(r[0] for r in db.execute(
                "SELECT source_rowid FROM effect_receipts_r62_duplicates "
                "WHERE classification IN ('strict','quarantine','duplicate')"
            ).fetchall())
            assert three_group == orig, (
                f"三组并集 != original\nmissing: {orig - three_group}\n"
                f"extra: {three_group - orig}"
            )
        finally:
            db.close()

    def test_original_data_reconstructable_from_strict_and_quarantine(self):
        """strict + quarantine 的数据可重建 original 的合法+非法行(无字段丢失)。

        strict 分类行:数据已迁移到 strict 表(按 a,e,t,rh,created_at 匹配)。
        quarantine 分类行:数据已迁移到 quarantine 表(含 quarantine_reason)。
        duplicate 分类行:loser 数据保留在 original 表(可从 original 取)。
        """
        db, _ = _build_migrated_mixed_db()
        try:
            # strict 分类的行数据已在 strict 表
            strict_evidence = db.execute(
                "SELECT d.source_rowid, d.action_id, d.effect_type, d.target, "
                "       d.request_hash, o.created_at "
                "FROM effect_receipts_r62_duplicates d "
                "JOIN effect_receipts_invalid_r62 o ON o.rowid = d.source_rowid "
                "WHERE d.classification = 'strict'"
            ).fetchall()
            for src, aid, etype, tgt, rh, created in strict_evidence:
                cnt = db.execute(
                    "SELECT COUNT(*) FROM effect_receipts "
                    "WHERE action_id=? AND effect_type=? AND target=? "
                    "  AND request_hash=? AND created_at=?",
                    (aid, etype, tgt, rh, created),
                ).fetchone()[0]
                assert cnt == 1, (
                    f"strict rowid {src} 数据未在 strict 表: cnt={cnt}"
                )
            # quarantine 分类的行数据已在 quarantine 表
            quar_evidence = db.execute(
                "SELECT d.source_rowid, d.action_id, d.effect_type, d.target, "
                "       o.created_at "
                "FROM effect_receipts_r62_duplicates d "
                "JOIN effect_receipts_invalid_r62 o ON o.rowid = d.source_rowid "
                "WHERE d.classification = 'quarantine'"
            ).fetchall()
            for src, aid, etype, tgt, created in quar_evidence:
                cnt = db.execute(
                    "SELECT COUNT(*) FROM effect_receipts_r62_quarantine "
                    "WHERE action_id IS ? AND effect_type IS ? AND target IS ? "
                    "  AND created_at IS ?",
                    (aid, etype, tgt, created),
                ).fetchone()[0]
                assert cnt == 1, (
                    f"quarantine rowid {src} 数据未在 quarantine 表: cnt={cnt}"
                )
        finally:
            db.close()

    def test_transactional_rollback_restores_pre_migration_state(self):
        """migrate.py 在 BEGIN IMMEDIATE 事务中执行 migration;ROLLBACK 后 DB 回到原状。

        验证:执行所有 migration 语句后 ROLLBACK,effect_receipts 仍是旧表(9 行),
        无 strict/quarantine/duplicates 残留(SQLite 支持事务性 DDL)。
        这保证 migration 失败回滚后重跑可从干净状态开始(幂等)。
        """
        try:
            from database.migrate import _split_sql_statements
        except Exception as e:
            pytest.skip(f"database.migrate 不可导入: {e}")
        sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
        statements = _split_sql_statements(sql_text)
        assert len(statements) > 0, "migration SQL 拆分后无语句"
        db = sqlite3.connect(":memory:")
        _create_legacy_effect_receipts(db)
        _seed_mixed_data(db)
        assert db.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0] == 9
        # 在事务中执行所有语句
        db.execute("BEGIN IMMEDIATE")
        for stmt in statements:
            db.execute(stmt)
        db.execute("ROLLBACK")
        try:
            # 回滚后:effect_receipts 仍是旧表,9 行
            assert db.execute(
                "SELECT COUNT(*) FROM effect_receipts"
            ).fetchone()[0] == 9, "ROLLBACK 后 effect_receipts 应仍是旧表 9 行"
            # strict 表不存在(回滚了 CREATE + RENAME)
            assert db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name='effect_receipts_r62_strict'"
            ).fetchone()[0] == 0, "ROLLBACK 后不应有 effect_receipts_r62_strict"
            # quarantine 表不存在
            assert db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name='effect_receipts_r62_quarantine'"
            ).fetchone()[0] == 0, "ROLLBACK 后不应有 effect_receipts_r62_quarantine"
            # duplicates 表不存在
            assert db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name='effect_receipts_r62_duplicates'"
            ).fetchone()[0] == 0, "ROLLBACK 后不应有 effect_receipts_r62_duplicates"
            # invalid 表不存在(ROLLBACK 撤销 RENAME)
            assert db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name='effect_receipts_invalid_r62'"
            ).fetchone()[0] == 0, "ROLLBACK 后不应有 effect_receipts_invalid_r62"
        finally:
            db.close()


# ════════════════════════════════════════════════════════════════
# K. fail-closed — 守恒违反时 CHECK raise
# ════════════════════════════════════════════════════════════════

class TestFailClosedOnConservationViolation:
    """K 组:守恒等式违反时,SQL CHECK 约束 raise → 事务 ROLLBACK(fail-closed)。

    migration 004 的 Step 2f/2g 用 CREATE TABLE ... CHECK + INSERT CASE WHEN
    实现等式断言:等式不成立时 INSERT 0 违反 CHECK → IntegrityError。
    """

    def test_conservation_assertion_raises_on_mismatch(self):
        """守恒等式不成立时,SQL 断言 raise IntegrityError(fail-closed)。"""
        db = sqlite3.connect(":memory:")
        # 构造不匹配:original=1,但 strict/quarantine/duplicates 都=0
        db.execute("CREATE TABLE effect_receipts_r62_strict (x INTEGER)")
        db.execute("CREATE TABLE effect_receipts_r62_quarantine (x INTEGER)")
        db.execute("CREATE TABLE effect_receipts_r62_duplicates (classification TEXT)")
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
        try:
            with pytest.raises(sqlite3.IntegrityError):
                db.executescript(assert_sql)
        finally:
            db.close()

    def test_evidence_assertion_raises_on_missing_evidence(self):
        """证据完整性不成立时(取证表行数 != 原始行数),SQL 断言 raise。"""
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE effect_receipts_r62_duplicates (classification TEXT)")
        db.execute("CREATE TABLE effect_receipts (x INTEGER)")
        db.execute("INSERT INTO effect_receipts VALUES (1)")
        db.execute("INSERT INTO effect_receipts VALUES (2)")
        db.execute("INSERT INTO effect_receipts_r62_duplicates VALUES ('strict')")
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
        try:
            with pytest.raises(sqlite3.IntegrityError):
                db.executescript(assert_sql)
        finally:
            db.close()

    def test_full_migration_passes_assertions_on_valid_data(self):
        """合法数据下,完整 migration 的守恒+证据断言通过(不 raise)。"""
        db = sqlite3.connect(":memory:")
        _create_legacy_effect_receipts(db)
        _seed_mixed_data(db)
        try:
            # 应成功执行(不 raise)— 守恒+证据断言均通过
            _run_migration_004(db)
            # 再次验证等式成立
            original = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
            ).fetchone()[0]
            strict = db.execute(
                "SELECT COUNT(*) FROM effect_receipts"
            ).fetchone()[0]
            quarantine = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
            ).fetchone()[0]
            dup = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
                "WHERE classification='duplicate'"
            ).fetchone()[0]
            evidence = db.execute(
                "SELECT COUNT(*) FROM effect_receipts_r62_duplicates"
            ).fetchone()[0]
            assert strict + quarantine + dup == original == 9
            assert evidence == original == 9
        finally:
            db.close()


# ════════════════════════════════════════════════════════════════
# L. Python 防御纵深断言(_assert_migration_fingerprint)
# ════════════════════════════════════════════════════════════════

class TestPythonDefenseInDepth:
    """L 组:migrate.py `_assert_migration_fingerprint` 的 004 守恒断言(防御纵深)。

    验证 Python 层在 COMMIT 前再次比对四组 COUNT,即使 SQL 层断言被绕过,
    Python 层仍能捕获守恒/证据完整性违反(raise RuntimeError → ROLLBACK)。
    """

    @pytest.mark.asyncio
    async def test_python_assertion_passes_on_conserved_migration(self):
        """干净 migration 后,Python 守恒断言通过(不 raise)。"""
        import aiosqlite
        try:
            from database.migrate import _assert_migration_fingerprint
        except Exception as e:
            pytest.skip(f"database.migrate 不可导入: {e}")

        db_path = None
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="r64_p1_3_py_ok_")
        try:
            db_path = Path(tmpdir) / "py.db"
            async with aiosqlite.connect(str(db_path)) as db:
                await _create_legacy_effect_receipts_async(db)
                await _seed_mixed_data_async(db)
                sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
                await db.executescript(sql_text)
                await db.commit()
                # 应通过(不 raise)
                await _assert_migration_fingerprint(
                    db, "004_effect_receipts_request_hash_unique.sql"
                )
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_python_assertion_raises_on_strict_loss(self):
        """删除 strict 行后,Python 守恒断言 raise RuntimeError(行丢失)。"""
        import aiosqlite
        try:
            from database.migrate import _assert_migration_fingerprint
        except Exception as e:
            pytest.skip(f"database.migrate 不可导入: {e}")

        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="r64_p1_3_py_loss_")
        try:
            db_path = Path(tmpdir) / "py.db"
            async with aiosqlite.connect(str(db_path)) as db:
                await _create_legacy_effect_receipts_async(db)
                await _seed_mixed_data_async(db)
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
            import shutil
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
    await db.execute(_LEGACY_DDL)
    await db.commit()


async def _insert_legacy_row_async(
    db,
    *,
    action_id: str,
    effect_type: str = "telegram_send",
    target: str = "t-default",
    status: str = "pending",
    created_at: str | None = "2026-01-01T00:00:00Z",
    request_hash=_UNSET,
    attempt: int | None = 0,
) -> int:
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


async def _seed_mixed_data_async(db) -> dict:
    """aiosqlite 版 _seed_mixed_data(相同数据布局)。"""
    rh_a3 = _valid_hash("a3grp")
    rh_a6 = _valid_hash("a6grp")
    meta: dict = {"rows": [], "groups": {}}

    r1 = await _insert_legacy_row_async(
        db, action_id="a1", target="t1",
        request_hash=_valid_hash("a1"), created_at="2026-01-01T00:00:00Z")
    meta["rows"].append({"rowid": r1, "expect": "strict", "group": "a1"})

    r2 = await _insert_legacy_row_async(
        db, action_id="a2", target="t2",
        request_hash=_valid_hash("a2"), created_at="2026-01-02T00:00:00Z")
    meta["rows"].append({"rowid": r2, "expect": "strict", "group": "a2"})

    r3a = await _insert_legacy_row_async(
        db, action_id="a3", target="t3", request_hash=rh_a3,
        created_at="2026-01-03T00:00:00Z", status="pending")
    r3b = await _insert_legacy_row_async(
        db, action_id="a3", target="t3", request_hash=rh_a3,
        created_at="2026-01-05T00:00:00Z", status="completed")
    r3c = await _insert_legacy_row_async(
        db, action_id="a3", target="t3", request_hash=rh_a3,
        created_at="2026-01-04T00:00:00Z", status="failed")
    meta["rows"].append({"rowid": r3a, "expect": "duplicate", "group": "a3"})
    meta["rows"].append({"rowid": r3b, "expect": "strict", "group": "a3"})
    meta["rows"].append({"rowid": r3c, "expect": "duplicate", "group": "a3"})
    meta["groups"]["a3"] = {"winner": r3b, "losers": [r3a, r3c], "rh": rh_a3}

    r4 = await _insert_legacy_row_async(
        db, action_id="a4", target="t4", request_hash="",
        created_at="2026-01-06T00:00:00Z")
    r5 = await _insert_legacy_row_async(
        db, action_id="a5", target="t5", request_hash=None, created_at=None)
    meta["rows"].append({"rowid": r4, "expect": "quarantine", "group": "a4"})
    meta["rows"].append({"rowid": r5, "expect": "quarantine", "group": "a5"})

    r6a = await _insert_legacy_row_async(
        db, action_id="a6", target="t6", request_hash=rh_a6,
        created_at="2026-01-07T00:00:00Z", status="pending")
    r6b = await _insert_legacy_row_async(
        db, action_id="a6", target="t6", request_hash=rh_a6,
        created_at="2026-01-07T00:00:00Z", status="completed")
    meta["rows"].append({"rowid": r6a, "expect": "duplicate", "group": "a6"})
    meta["rows"].append({"rowid": r6b, "expect": "strict", "group": "a6"})
    meta["groups"]["a6"] = {"winner": r6b, "losers": [r6a], "rh": rh_a6}

    return meta
