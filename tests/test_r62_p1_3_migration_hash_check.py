"""R62 P1-03: 003 migration 的 request_hash CHECK 完整十六进制校验测试。

审计背景(R62 终审报告 §5 P1-03):
  旧实现 `CHECK (length(request_hash) = 64 AND request_hash GLOB '[0-9a-f]*')` 中,
  SQLite GLOB '[0-9a-f]*' 仅校验首字符属于十六进制集合,后 63 字符可任意。
  这意味着 'a' + 63 个 '$' 之类的非法 hash 可通过 CHECK 约束,混入 strict 表。

  新实现 `CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*')`
  保证全部 64 字符均为小写十六进制 [0-9a-f]。NOT GLOB '*[^0-9a-f]*' 语义为:
  "字符串中不包含任何非 [0-9a-f] 字符"。

测试覆盖矩阵(11 个用例):
  A. 合法 hash 接受 (2)
     1.  test_valid_lowercase_hex_accepted: 标准 64 字符小写 hex 通过
     2.  test_all_zero_hash_accepted: 全 0 hash 通过(边界值)

  B. 非法 hash 拒绝 (8)
     3.  test_old_bypass_pattern_rejected: 'a' + 63 个 '$' 被拒绝(R62 核心)
     4.  test_uppercase_hex_rejected: 大写 hex 'A-F' 被拒绝(GLOB 大小写敏感)
     5.  test_non_ascii_rejected: 含中文/emoji 的 hash 被拒绝
     6.  test_illegal_at_start_rejected: 首字符非法('g' 开头)
     7.  test_illegal_in_middle_rejected: 中间字符非法(第 32 位为 'g')
     8.  test_illegal_at_end_rejected: 尾字符非法(第 64 位为 'g')
     9.  test_wrong_length_too_short_rejected: 63 字符被拒绝
     10. test_wrong_length_too_long_rejected: 65 字符被拒绝

  C. quarantine 隔离验证 (1)
     11. test_invalid_hash_quarantined_not_dropped: 非法 hash 行隔离到 quarantine 表

设计说明:
- 测试直接在临时 SQLite 上执行 003 migration 的关键 DDL + DML,验证 CHECK 约束行为。
- 使用 aiosqlite/sqlite3 原生执行,不依赖 CacheStore(隔离 DDL 层行为)。
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
MIGRATION_SQL = REPO_ROOT / "database" / "migrations" / "003_rebuild_command_approvals.sql"


def _create_legacy_command_approvals(db: sqlite3.Connection) -> None:
    """创建旧格式 command_approvals 表(模拟 001_initial_schema 的宽松约束)。

    旧表无 CHECK 约束,允许任意 request_hash 值,用于测试 003 migration 的过滤能力。
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS command_approvals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id       TEXT,
            approver_id     BIGINT,
            approval_type   TEXT,
            decision        TEXT,
            request_hash    TEXT,
            mfa_receipt     TEXT,
            permission      TEXT,
            approved_at     TEXT,
            expires_at      TEXT,
            consumed_at     TEXT,
            revoked_at      TEXT,
            metadata_json   TEXT
        )
    """)
    db.commit()


def _run_migration_003(db: sqlite3.Connection) -> None:
    """执行 003 migration SQL(在单个事务中)。

    读取 migration SQL 文件并 executescript(模拟 migrate.py 的 BEGIN IMMEDIATE 包裹)。
    """
    if not MIGRATION_SQL.exists():
        pytest.skip(f"migration SQL not found: {MIGRATION_SQL}")
    sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
    # executescript 自动提交当前事务再执行脚本;脚本内部多条语句依次执行
    db.executescript(sql_text)
    db.commit()


def _valid_hex() -> str:
    """返回一个合法的 64 字符小写十六进制字符串。"""
    return "0123456789abcdef" * 4  # 64 chars


@pytest.fixture
def db_with_legacy_data():
    """创建临时 SQLite DB,含旧格式 command_approvals 表(空)。

    调用方负责 INSERT 旧行后调用 _run_migration_003。
    """
    tmpdir = tempfile.mkdtemp(prefix="r62_p1_3_test_")
    db_path = Path(tmpdir) / "test_migration.db"
    db = sqlite3.connect(str(db_path))
    _create_legacy_command_approvals(db)
    try:
        yield db
    finally:
        db.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 辅助:插入旧行并执行 migration
# ════════════════════════════════════════════════════════════════

def _insert_legacy_row(
    db: sqlite3.Connection,
    *,
    id_: int,
    request_hash: str | None,
    mfa_receipt: str = "receipt-abc",
    permission: str = "break_glass",
    expires_at: str = "2026-12-31T23:59:59Z",
    decision: str = "approved",
    approval_type: str = "break_glass",
) -> None:
    """向旧 command_approvals 表插入一行(宽松约束,允许任意 request_hash)。"""
    db.execute(
        "INSERT INTO command_approvals "
        "(id, action_id, approver_id, approval_type, decision, request_hash, "
        " mfa_receipt, permission, approved_at, expires_at, consumed_at, revoked_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id_, f"action-{id_}", 1001, approval_type, decision, request_hash,
            mfa_receipt, permission, "2026-01-01T00:00:00Z", expires_at, None, None, None,
        ),
    )
    db.commit()


# ════════════════════════════════════════════════════════════════
# A. 合法 hash 接受
# ════════════════════════════════════════════════════════════════

class TestValidHashAccepted:
    """A 组:合法的 64 字符小写十六进制 hash 应被 strict 表接受。"""

    def test_valid_lowercase_hex_accepted(self, db_with_legacy_data):
        """标准 64 字符小写 hex 通过 CHECK 约束,进入 strict 表。"""
        db = db_with_legacy_data
        _insert_legacy_row(db, id_=1, request_hash=_valid_hex())
        _run_migration_003(db)
        count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (_valid_hex(),),
        ).fetchone()[0]
        assert count == 1, "合法小写 hex hash 应迁移到 strict 表"

    def test_all_zero_hash_accepted(self, db_with_legacy_data):
        """全 '0' hash(边界值)通过 CHECK 约束。"""
        db = db_with_legacy_data
        all_zero = "0" * 64
        _insert_legacy_row(db, id_=1, request_hash=all_zero)
        _run_migration_003(db)
        count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (all_zero,),
        ).fetchone()[0]
        assert count == 1, "全 0 hash 应通过(全是合法十六进制字符)"


# ════════════════════════════════════════════════════════════════
# B. 非法 hash 拒绝
# ════════════════════════════════════════════════════════════════

class TestInvalidHashRejected:
    """B 组:非法 hash 应被 strict 表 CHECK 约束拒绝,隔离到 quarantine 表。

    核心验证点:旧 GLOB '[0-9a-f]*' 仅校验首字符,新 NOT GLOB '*[^0-9a-f]*' 校验全部字符。
    """

    def test_old_bypass_pattern_rejected(self, db_with_legacy_data):
        """R62 核心:'a' + 63 个 '$' 旧实现可通过,新实现必须拒绝。"""
        db = db_with_legacy_data
        bypass_hash = "a" + "$" * 63  # 首字符合法,后 63 字符非法
        _insert_legacy_row(db, id_=1, request_hash=bypass_hash)
        _run_migration_003(db)
        # strict 表不应包含此行
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (bypass_hash,),
        ).fetchone()[0]
        assert strict_count == 0, (
            "R62 P1-03: 'a' + 63×'$' 必须被新 CHECK 拒绝(旧 GLOB '[0-9a-f]*' 会放行)"
        )
        # quarantine 表应包含此行(取证)
        quarantine_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals_r60_quarantine WHERE request_hash = ?",
            (bypass_hash,),
        ).fetchone()[0]
        assert quarantine_count == 1, "非法 hash 行应隔离到 quarantine 表"

    def test_uppercase_hex_rejected(self, db_with_legacy_data):
        """大写 hex 'A-F' 被拒绝(GLOB 大小写敏感,[0-9a-f] 不含大写)。"""
        db = db_with_legacy_data
        upper_hash = "A" * 64  # 全大写
        _insert_legacy_row(db, id_=1, request_hash=upper_hash)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (upper_hash,),
        ).fetchone()[0]
        assert strict_count == 0, "大写 hex 应被拒绝(GLOB [0-9a-f] 大小写敏感)"

    def test_non_ascii_rejected(self, db_with_legacy_data):
        """含中文/emoji 的 hash 被拒绝。"""
        db = db_with_legacy_data
        # 64 字符但含中文(每个中文字符长度为 1 但非 ASCII)
        non_ascii_hash = "a" * 32 + "测" * 32
        _insert_legacy_row(db, id_=1, request_hash=non_ascii_hash)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (non_ascii_hash,),
        ).fetchone()[0]
        assert strict_count == 0, "含非 ASCII 字符的 hash 应被拒绝"

    def test_illegal_at_start_rejected(self, db_with_legacy_data):
        """首字符非法('g' 开头)被拒绝。"""
        db = db_with_legacy_data
        hash_start_bad = "g" + "0" * 63  # 'g' 不在 [0-9a-f]
        _insert_legacy_row(db, id_=1, request_hash=hash_start_bad)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (hash_start_bad,),
        ).fetchone()[0]
        assert strict_count == 0, "首字符 'g' 应被拒绝"

    def test_illegal_in_middle_rejected(self, db_with_legacy_data):
        """中间字符非法(第 32 位为 'g')被拒绝。"""
        db = db_with_legacy_data
        hash_mid_bad = "0" * 32 + "g" + "0" * 31  # 第 33 字符为 'g'
        _insert_legacy_row(db, id_=1, request_hash=hash_mid_bad)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (hash_mid_bad,),
        ).fetchone()[0]
        assert strict_count == 0, "中间字符 'g' 应被拒绝"

    def test_illegal_at_end_rejected(self, db_with_legacy_data):
        """尾字符非法(第 64 位为 'g')被拒绝。"""
        db = db_with_legacy_data
        hash_end_bad = "0" * 63 + "g"  # 尾字符 'g'
        _insert_legacy_row(db, id_=1, request_hash=hash_end_bad)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (hash_end_bad,),
        ).fetchone()[0]
        assert strict_count == 0, "尾字符 'g' 应被拒绝"

    def test_wrong_length_too_short_rejected(self, db_with_legacy_data):
        """63 字符 hash 被拒绝(length != 64)。"""
        db = db_with_legacy_data
        short_hash = "0" * 63
        _insert_legacy_row(db, id_=1, request_hash=short_hash)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (short_hash,),
        ).fetchone()[0]
        assert strict_count == 0, "63 字符 hash 应被拒绝(length=64 约束)"

    def test_wrong_length_too_long_rejected(self, db_with_legacy_data):
        """65 字符 hash 被拒绝(length != 64)。"""
        db = db_with_legacy_data
        long_hash = "0" * 65
        _insert_legacy_row(db, id_=1, request_hash=long_hash)
        _run_migration_003(db)
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (long_hash,),
        ).fetchone()[0]
        assert strict_count == 0, "65 字符 hash 应被拒绝(length=64 约束)"


# ════════════════════════════════════════════════════════════════
# C. quarantine 隔离 + 守恒验证
# ════════════════════════════════════════════════════════════════

class TestQuarantineAndConservation:
    """C 组:非法行隔离到 quarantine 表,守恒断言 strict+quarantine=original。"""

    def test_invalid_hash_quarantined_not_dropped(self, db_with_legacy_data):
        """非法 hash 行不丢失,隔离到 quarantine 表并标注原因。"""
        db = db_with_legacy_data
        bypass_hash = "a" + "$" * 63
        _insert_legacy_row(db, id_=1, request_hash=bypass_hash)
        _run_migration_003(db)
        row = db.execute(
            "SELECT request_hash, quarantine_reason FROM command_approvals_r60_quarantine "
            "WHERE id = 1"
        ).fetchone()
        assert row is not None, "非法行应隔离到 quarantine 表(不丢失)"
        assert row[0] == bypass_hash
        assert row[1] == "invalid_request_hash", (
            f"quarantine_reason 应为 'invalid_request_hash',实际 '{row[1]}'"
        )

    def test_conservation_strict_plus_quarantine_equals_original(self, db_with_legacy_data):
        """守恒断言:strict + quarantine = original(混合合法+非法行)。"""
        db = db_with_legacy_data
        # 3 行合法 + 3 行非法
        _insert_legacy_row(db, id_=1, request_hash=_valid_hex())
        _insert_legacy_row(db, id_=2, request_hash="0" * 64)
        _insert_legacy_row(db, id_=3, request_hash="a" + "b" * 63)  # 合法
        _insert_legacy_row(db, id_=4, request_hash="a" + "$" * 63)  # 非法
        _insert_legacy_row(db, id_=5, request_hash="g" * 64)  # 非法
        _insert_legacy_row(db, id_=6, request_hash="A" * 64)  # 非法(大写)
        # migration 前 command_approvals 是旧表(宽松约束)
        original_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals"
        ).fetchone()[0]
        assert original_count == 6
        _run_migration_003(db)
        # migration 后 command_approvals 是 strict 表
        strict_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals"
        ).fetchone()[0]
        quarantine_count = db.execute(
            "SELECT COUNT(*) FROM command_approvals_r60_quarantine"
        ).fetchone()[0]
        assert strict_count == 3, f"合法行应为 3,实际 {strict_count}"
        assert quarantine_count == 3, f"非法行应为 3,实际 {quarantine_count}"
        assert strict_count + quarantine_count == original_count, "守恒断言失败:行丢失"


# ════════════════════════════════════════════════════════════════
# D. CHECK 约束运行时插入验证
# ════════════════════════════════════════════════════════════════

class TestCheckConstraintRuntimeEnforcement:
    """D 组:migration 后的 strict 表 CHECK 约束在运行时插入时也生效。

    验证不仅迁移时过滤,后续 INSERT 也被 CHECK 约束拦截。
    """

    def test_runtime_insert_invalid_hash_rejected_by_check(self, db_with_legacy_data):
        """migration 后,直接 INSERT 非法 hash 应被 CHECK 约束拒绝(raise)。"""
        db = db_with_legacy_data
        _insert_legacy_row(db, id_=1, request_hash=_valid_hex())
        _run_migration_003(db)
        # 尝试插入非法 hash — 应被 CHECK 拒绝
        bypass_hash = "a" + "$" * 63
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            db.execute(
                "INSERT INTO command_approvals "
                "(id, action_id, approver_id, approval_type, decision, request_hash, "
                " mfa_receipt, permission, approved_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (99, "action-99", 1001, "break_glass", "approved", bypass_hash,
                 "receipt", "break_glass", "2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z"),
            )
        assert "CHECK" in str(exc_info.value).upper() or "CONSTRAINT" in str(exc_info.value).upper(), (
            f"应触发 CHECK 约束失败,实际: {exc_info.value}"
        )

    def test_runtime_insert_valid_hash_accepted(self, db_with_legacy_data):
        """migration 后,INSERT 合法 hash 成功通过 CHECK。"""
        db = db_with_legacy_data
        _insert_legacy_row(db, id_=1, request_hash=_valid_hex())
        _run_migration_003(db)
        new_hash = "f" * 64
        db.execute(
            "INSERT INTO command_approvals "
            "(id, action_id, approver_id, approval_type, decision, request_hash, "
            " mfa_receipt, permission, approved_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (99, "action-99", 1001, "break_glass", "approved", new_hash,
             "receipt", "break_glass", "2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z"),
        )
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM command_approvals WHERE request_hash = ?",
            (new_hash,),
        ).fetchone()[0]
        assert count == 1
