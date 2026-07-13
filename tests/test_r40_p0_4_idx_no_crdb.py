"""R40 P0-4: Idx Bot 不再依赖 CRDB 凭证 — AST 门禁 + CAS 行为测试。

门禁检查(ast.parse + ast.walk):
1. idx_bot.py 不含 get_pending_uploads_col 的导入或调用(已迁移到 SQLite)
2. idx_bot.py 不含任何 get_*_col 模式的函数调用(避免运行时直连 CRDB Collection)
3. idx_bot.py 调用了 claim_pending_uploads(SQLite CAS 认领)
4. idx_bot.py 调用了 complete_pending_upload / fail_pending_upload(状态推进)
5. idx_bot.py 在 _process_one_pending 中使用 writer_transaction(同事务原子写入)

CAS 行为测试:
- claim_pending_uploads 原子认领后,第二次调用不再返回相同记录
- complete_pending_upload 标记 processed=1,不再被认领
- fail_pending_upload 回滚 processed=0,允许下轮重领
- reset_stale_claims 重置超时 claimed 记录回 pending
- writer_transaction 包裹 complete_pending_upload,失败时整体 ROLLBACK
"""
import ast
import inspect
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

# ── 模块级 skip 检查 ────────────────────────────────────────────
# conftest.py 在 cache_store 不可导入时会注入 MagicMock 占位,
# 此时 CacheStore 不是真实类,整文件 skip 以避免误导。
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore

# idx_bot.py 源码路径(相对项目根目录)
_IDX_BOT_PATH = Path(__file__).resolve().parent.parent / "bots" / "idx_bot.py"


# ════════════════════════════════════════════════════════════════
# Part 1: AST 门禁检查(同步,纯静态分析)
# ════════════════════════════════════════════════════════════════

def _parse_idx_bot() -> ast.Module:
    """读取并解析 idx_bot.py 源码为 AST。"""
    source = _IDX_BOT_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(_IDX_BOT_PATH))


def _all_call_names(tree: ast.Module) -> list[str]:
    """提取 AST 中所有函数调用名(仅 Name.func 形式,不含属性链)。

    例如 ``get_pending_uploads_col()`` → "get_pending_uploads_col"
    ``store.claim_pending_uploads(...)`` → "claim_pending_uploads"(属性调用)
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    return names


def _all_imported_names(tree: ast.Module) -> list[str]:
    """提取 AST 中所有 from ... import 的名称。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.append(alias.asname or alias.name)
    return names


class TestAstGateIdxNoCrdb:
    """AST 静态门禁:确保 idx_bot.py 不再直连 CRDB Collection。"""

    def test_no_get_pending_uploads_col_import(self):
        """门禁 1: idx_bot.py 不再导入 get_pending_uploads_col。"""
        tree = _parse_idx_bot()
        imported = _all_imported_names(tree)
        assert "get_pending_uploads_col" not in imported, (
            "idx_bot.py 仍导入 get_pending_uploads_col,"
            "R40 P0-4 要求完全迁移到 SQLite,不再依赖 CRDB Collection"
        )

    def test_no_get_pending_uploads_col_call(self):
        """门禁 2: idx_bot.py 不再调用 get_pending_uploads_col()。"""
        tree = _parse_idx_bot()
        calls = _all_call_names(tree)
        assert "get_pending_uploads_col" not in calls, (
            "idx_bot.py 仍调用 get_pending_uploads_col(),"
            "R40 P0-4 要求改用 store.claim_pending_uploads() / complete / fail"
        )

    def test_no_get_any_col_calls_in_pending_functions(self):
        """门禁 3: _process_pending_uploads / _process_one_pending 中不含 get_*_col 调用。

        get_file_records_col / get_codes_col / get_decode_logs_col 等函数
        返回 CRDB Collection,调用即触发运行时 CRDB 连接。
        R40 P0-4 要求 pending_uploads 处理流程完全走 SQLite CacheStore,
        不依赖 CRDB 凭证(Idx Bot 其他解码功能仍可使用 CRDB)。
        """
        import re
        tree = _parse_idx_bot()
        pattern = re.compile(r"^get_\w+_col$")

        # 只检查 _process_pending_uploads 和 _process_one_pending 两个函数
        target_func_names = {"_process_pending_uploads", "_process_one_pending"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in target_func_names:
                    # 在该函数体内查找 get_*_col 调用
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            func = child.func
                            if isinstance(func, ast.Name) and pattern.match(func.id):
                                offenders.append(func.id)
                            elif isinstance(func, ast.Attribute) and pattern.match(func.attr):
                                offenders.append(func.attr)
        assert not offenders, (
            f"_process_pending_uploads / _process_one_pending 仍调用 CRDB Collection 函数: {offenders},"
            "R40 P0-4 要求 pending_uploads 处理流程完全走 SQLite CacheStore"
        )

    def test_claim_pending_uploads_is_called(self):
        """门禁 4: idx_bot.py 调用了 claim_pending_uploads(SQLite CAS 认领)。"""
        tree = _parse_idx_bot()
        calls = _all_call_names(tree)
        assert "claim_pending_uploads" in calls, (
            "idx_bot.py 未调用 store.claim_pending_uploads(),"
            "_process_pending_uploads 应改用 SQLite CAS 认领替代 get_pending_uploads_col().find()"
        )

    def test_complete_pending_upload_is_called(self):
        """门禁 5: idx_bot.py 调用了 complete_pending_upload(标记完成)。"""
        tree = _parse_idx_bot()
        calls = _all_call_names(tree)
        assert "complete_pending_upload" in calls, (
            "idx_bot.py 未调用 store.complete_pending_upload(),"
            "处理成功后应标记 pending_upload 为 processed=1"
        )

    def test_fail_pending_upload_is_called(self):
        """门禁 6: idx_bot.py 调用了 fail_pending_upload(标记失败/回滚)。"""
        tree = _parse_idx_bot()
        calls = _all_call_names(tree)
        assert "fail_pending_upload" in calls, (
            "idx_bot.py 未调用 store.fail_pending_upload(),"
            "处理失败时应回滚 pending_upload 为 processed=0 允许重领"
        )

    def test_writer_transaction_used_in_process_one_pending(self):
        """门禁 7: _process_one_pending 中使用 writer_transaction(同事务原子写入)。

        R40 P0-4 要求 file_records_local + codes_local + dirty_outbox +
        complete_pending_upload 在同一 SQLite 事务中提交,失败时整体 ROLLBACK。
        """
        tree = _parse_idx_bot()
        # 查找 _process_one_pending 函数定义
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "_process_one_pending":
                    target_func = node
                    break
        assert target_func is not None, "idx_bot.py 中找不到 _process_one_pending 函数"

        # 在该函数体内查找 writer_transaction 调用
        found_writer_tx = False
        for child in ast.walk(target_func):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == "writer_transaction":
                    found_writer_tx = True
                    break
        assert found_writer_tx, (
            "_process_one_pending 未使用 store.writer_transaction(),"
            "file_records + codes + complete_pending_upload 必须在同一事务中"
        )

    def test_no_pending_col_variable_usage(self):
        """门禁 8: idx_bot.py 不含 pending_col 变量的方法调用。

        原代码使用 ``pending_col = get_pending_uploads_col()`` 然后
        ``pending_col.update_one(...)``。R40 P0-4 要求删除所有 pending_col 用法。
        本检查扫描所有形如 ``pending_col.xxx()`` 的属性调用。
        """
        tree = _parse_idx_bot()
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id == "pending_col":
                        offenders.append(func.attr)
        assert not offenders, (
            f"idx_bot.py 仍使用 pending_col.{offenders} 调用 CRDB Collection,"
            "R40 P0-4 要求改用 store.complete_pending_upload / fail_pending_upload"
        )


# ════════════════════════════════════════════════════════════════
# Part 2: CAS 行为测试(异步,真实 SQLite 临时数据库)
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离于生产 cache_store.db)。

    策略:
    1. 临时目录下的 test_p0_4.db(避免污染项目 data/cache_store.db)。
    2. monkeypatch 替换 database.cache_store.DB_PATH 模块属性。
    3. 结束后 close + shutil.rmtree。
    """
    tmpdir = tempfile.mkdtemp(prefix="r40_p0_4_test_")
    db_path = Path(tmpdir) / "test_p0_4.db"
    original_path = _cs_module.DB_PATH
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


def _make_pending_record(uploader_id: int = 100, channel_id: int = 200,
                          message_id: int = 300, upload_id: str = "") -> dict:
    """构造一条 pending_upload 记录(用于 insert_pending_upload_local)。"""
    return {
        "uploader_id": uploader_id,
        "primary_channel_id": channel_id,
        "primary_channel_msg_id": message_id,
        "file_types": {"document": 1},
        "batch_msg_ids": "301,302",
        "batch_file_meta": [{"type": "document", "file_id": "xxx"}],
        "note": "",
        "protect_content": 0,
        "file_ttl_days": 0,
        "upload_id": upload_id,
        "status_msg_id": 0,
        "created_at": "2026-07-13T00:00:00+00:00",
    }


class TestCasClaimPendingUploads:
    """claim_pending_uploads CAS 认领行为测试。"""

    @pytest.mark.asyncio
    async def test_claim_returns_pending_records(self, store):
        """claim_pending_uploads 返回 processed=0 的记录并原子置为 processed=2。"""
        # 插入 3 条 pending 记录
        for i in range(3):
            rec = _make_pending_record(
                uploader_id=100 + i,
                message_id=300 + i,
                upload_id=f"upload-00{i}",
            )
            new_id = await store.insert_pending_upload_local(rec)
            assert new_id > 0, f"insert_pending_upload_local 失败 i={i}"

        # claim 2 条
        cutoff = time.time()  # 当前时间,claimed_at < cutoff 的记录可被认领
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=2)
        assert len(claimed) == 2, f"应认领 2 条,实际 {len(claimed)}"

        # 验证返回的记录字段
        for row in claimed:
            assert row["processed"] == 2, f"认领后 processed 应为 2,实际 {row['processed']}"
            assert row["claimed_at"] > 0, "认领后 claimed_at 应 > 0"
            assert "uploader_id" in row
            assert "primary_channel_id" in row
            assert "file_types" in row

    @pytest.mark.asyncio
    async def test_claimed_records_not_reclaimed(self, store):
        """CAS 核心:已认领(processed=2)的记录不会被再次认领。"""
        rec = _make_pending_record(uploader_id=200, message_id=400)
        await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        # 第一次认领
        first_claim = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(first_claim) == 1, "第一次应认领 1 条"

        # 第二次认领(同一 cutoff,已认领的记录 claimed_at > 0 且不 < cutoff)
        # claim_pending_uploads 的 WHERE 条件: processed=0 AND (claimed_at < cutoff OR claimed_at=0)
        # 已认领的记录 processed=2,不满足 processed=0,不会被再次选中
        second_claim = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(second_claim) == 0, (
            f"CAS 失败:已认领的记录被再次认领(第二次返回 {len(second_claim)} 条),"
            "claim_pending_uploads 必须保证每条记录只被一个 worker 拿到"
        )

    @pytest.mark.asyncio
    async def test_claim_limit_respected(self, store):
        """claim_pending_uploads 严格遵守 limit 参数。"""
        for i in range(5):
            rec = _make_pending_record(
                uploader_id=300 + i, message_id=500 + i, upload_id=f"upload-lim-{i}"
            )
            await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=3)
        assert len(claimed) == 3, f"limit=3 应返回 3 条,实际 {len(claimed)}"

        # 剩余 2 条可被认领
        remaining = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(remaining) == 2, f"剩余应 2 条,实际 {len(remaining)}"

    @pytest.mark.asyncio
    async def test_complete_marks_processed_1(self, store):
        """complete_pending_upload 标记 processed=1,记录不再被认领。"""
        rec = _make_pending_record(uploader_id=400, message_id=600)
        new_id = await store.insert_pending_upload_local(rec)
        assert new_id > 0

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(claimed) == 1
        pend_id = claimed[0]["id"]

        # 标记完成
        ok = await store.complete_pending_upload(pend_id)
        assert ok is True, "complete_pending_upload 应返回 True"

        # 再次 claim,不应返回已完成的记录
        again = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(again) == 0, "已完成的记录(processed=1)不应被再次认领"

    @pytest.mark.asyncio
    async def test_fail_rolls_back_to_pending(self, store):
        """fail_pending_upload 回滚 processed=0,允许下轮重领。"""
        rec = _make_pending_record(uploader_id=500, message_id=700)
        new_id = await store.insert_pending_upload_local(rec)
        assert new_id > 0

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(claimed) == 1
        pend_id = claimed[0]["id"]

        # 标记失败
        ok = await store.fail_pending_upload(pend_id, "test_failure")
        assert ok is True, "fail_pending_upload 应返回 True"

        # 失败后 processed=0, claimed_at=0,可被再次认领
        # 注意:claim 条件是 claimed_at < cutoff OR claimed_at=0,
        # fail 设置 claimed_at=0,所以 < cutoff 也成立(0 < cutoff)
        reclaimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(reclaimed) == 1, "fail 后记录应可被重新认领(at-least-once 语义)"
        assert reclaimed[0]["id"] == pend_id, "重新认领的应是同一条记录"

    @pytest.mark.asyncio
    async def test_fail_records_dead_reason_and_count(self, store):
        """fail_pending_upload 记录 dead_reason 和递增 dead_count。"""
        rec = _make_pending_record(uploader_id=600, message_id=800)
        new_id = await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        pend_id = claimed[0]["id"]

        # 第一次失败
        await store.fail_pending_upload(pend_id, "first_failure")
        # 第二次认领 + 失败
        reclaimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(reclaimed) == 1
        await store.fail_pending_upload(pend_id, "second_failure")

        # 验证 dead_count=2, dead_reason=second_failure
        rows = await store._db.execute_fetchall(
            "SELECT dead_count, dead_reason FROM pending_uploads_local WHERE id = ?",
            (pend_id,),
        )
        assert len(rows) == 1
        assert rows[0][0] == 2, f"dead_count 应为 2,实际 {rows[0][0]}"
        assert rows[0][1] == "second_failure", f"dead_reason 应为 second_failure,实际 {rows[0][1]}"

    @pytest.mark.asyncio
    async def test_reset_stale_claims(self, store):
        """reset_stale_claims 重置超时 claimed(processed=2)记录回 pending。"""
        rec = _make_pending_record(uploader_id=700, message_id=900)
        await store.insert_pending_upload_local(rec)

        # 认领
        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(claimed) == 1
        pend_id = claimed[0]["id"]

        # 手动将 claimed_at 设为很久以前(模拟崩溃超时)
        await store._db.execute(
            "UPDATE pending_uploads_local SET claimed_at = ? WHERE id = ?",
            (time.time() - 9999, pend_id),
        )
        await store._db.commit()

        # reset_stale_claims(超时 300 秒)
        reset_count = await store.reset_stale_claims(claim_timeout_seconds=300.0)
        assert reset_count == 1, f"应重置 1 条超时记录,实际 {reset_count}"

        # 重置后可被再次认领
        reclaimed = await store.claim_pending_uploads(cutoff_ts=time.time(), limit=10)
        assert len(reclaimed) == 1, "重置后记录应可被重新认领"
        assert reclaimed[0]["id"] == pend_id

    @pytest.mark.asyncio
    async def test_reset_stale_claims_does_not_touch_completed(self, store):
        """reset_stale_claims 不影响已完成的记录(processed=1)。"""
        rec = _make_pending_record(uploader_id=800, message_id=1000)
        new_id = await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        pend_id = claimed[0]["id"]
        await store.complete_pending_upload(pend_id)

        # 手动设 claimed_at 为很久以前(虽然 complete 已设 claimed_at=0)
        await store._db.execute(
            "UPDATE pending_uploads_local SET claimed_at = ? WHERE id = ?",
            (time.time() - 9999, pend_id),
        )
        await store._db.commit()

        reset_count = await store.reset_stale_claims(claim_timeout_seconds=300.0)
        # complete 后 processed=1,reset_stale_claims 只重置 processed=2 的记录
        assert reset_count == 0, "reset_stale_claims 不应影响 processed=1 的记录"

    @pytest.mark.asyncio
    async def test_empty_claim_returns_empty_list(self, store):
        """无 pending 记录时 claim 返回空列表。"""
        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert claimed == [], "无记录时应返回空列表"

    @pytest.mark.asyncio
    async def test_claim_file_types_deserialization(self, store):
        """claim 返回的 file_types / batch_file_meta 已反序列化为 dict/list。"""
        rec = _make_pending_record(uploader_id=900, message_id=1100)
        rec["file_types"] = {"photo": 2, "video": 1}
        rec["batch_file_meta"] = [{"type": "photo"}, {"type": "photo"}, {"type": "video"}]
        await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        assert len(claimed) == 1
        row = claimed[0]
        # file_types 应被反序列化为 dict
        assert isinstance(row["file_types"], dict), (
            f"file_types 应为 dict,实际 {type(row['file_types']).__name__}"
        )
        assert row["file_types"].get("photo") == 2
        # batch_file_meta 应被反序列化为 list
        assert isinstance(row["batch_file_meta"], list), (
            f"batch_file_meta 应为 list,实际 {type(row['batch_file_meta']).__name__}"
        )
        assert len(row["batch_file_meta"]) == 3


# ════════════════════════════════════════════════════════════════
# Part 3: writer_transaction 原子性测试
# ════════════════════════════════════════════════════════════════

class TestWriterTransactionAtomicity:
    """验证 writer_transaction 包裹 complete_pending_upload 的原子性。"""

    @pytest.mark.asyncio
    async def test_complete_in_writer_transaction_commits(self, store):
        """writer_transaction 内 complete_pending_upload 成功 → COMMIT 生效。"""
        rec = _make_pending_record(uploader_id=1100, message_id=1200)
        await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        pend_id = claimed[0]["id"]

        # 在 writer_transaction 中 complete
        async with store.writer_transaction():
            await store.complete_pending_upload(pend_id)

        # COMMIT 后,记录应为 processed=1
        rows = await store._db.execute_fetchall(
            "SELECT processed FROM pending_uploads_local WHERE id = ?", (pend_id,)
        )
        assert len(rows) == 1
        assert rows[0][0] == 1, f"COMMIT 后 processed 应为 1,实际 {rows[0][0]}"

    @pytest.mark.asyncio
    async def test_complete_in_writer_transaction_rollback_on_error(self, store):
        """writer_transaction 内异常 → ROLLBACK,complete 不生效。"""
        rec = _make_pending_record(uploader_id=1200, message_id=1300)
        await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        pend_id = claimed[0]["id"]
        # 认领后 processed=2, claimed_at>0

        # 在 writer_transaction 中 complete 然后抛异常
        with pytest.raises(RuntimeError, match="simulated_failure"):
            async with store.writer_transaction():
                await store.complete_pending_upload(pend_id)
                # 模拟后续操作失败
                raise RuntimeError("simulated_failure")

        # ROLLBACK 后,complete 应被撤销,processed 仍为 2(认领状态)
        rows = await store._db.execute_fetchall(
            "SELECT processed FROM pending_uploads_local WHERE id = ?", (pend_id,)
        )
        assert len(rows) == 1
        assert rows[0][0] == 2, (
            f"ROLLBACK 后 processed 应仍为 2(认领状态),实际 {rows[0][0]},"
            "说明 complete_pending_upload 未被回滚(事务隔离失败)"
        )

    @pytest.mark.asyncio
    async def test_fail_after_rollback(self, store):
        """事务 ROLLBACK 后,显式 fail_pending_upload 重置状态(独立事务)。"""
        rec = _make_pending_record(uploader_id=1300, message_id=1400)
        await store.insert_pending_upload_local(rec)

        cutoff = time.time()
        claimed = await store.claim_pending_uploads(cutoff_ts=cutoff, limit=10)
        pend_id = claimed[0]["id"]

        # 事务失败(模拟 file_records 写入失败)
        try:
            async with store.writer_transaction():
                await store.complete_pending_upload(pend_id)
                raise ValueError("db_write_failed")
        except ValueError:
            pass

        # ROLLBACK 后 processed=2(认领状态),需要显式 fail
        ok = await store.fail_pending_upload(pend_id, "db_write_failed")
        assert ok is True, "fail_pending_upload 应返回 True"

        # fail 后 processed=0, claimed_at=0,可被重新认领
        reclaimed = await store.claim_pending_uploads(cutoff_ts=time.time(), limit=10)
        assert len(reclaimed) == 1, "fail 后记录应可被重新认领"
        assert reclaimed[0]["id"] == pend_id
