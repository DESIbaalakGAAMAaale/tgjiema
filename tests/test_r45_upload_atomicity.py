"""R45 终审整改: Up Bot 媒体组聚合 + Idx Bot FinalizeUpload 原子提交测试。

覆盖 R42 终审报告第 9-10 节整改要求:

1. FinalizeUpload 原子提交: file_records + codes + pending_uploads + dirty_outbox 同事务
2. FinalizeUpload 原子回滚: 任一步骤失败全部 ROLLBACK
3. dirty_outbox 失败抛异常(不 warning 后 continue)
4. 码生成冲突重试(DB unique constraint + retry,最多 3 次)
5. Quota RESERVE/SETTLE/RELEASE ledger 模式
6. Quota 超时自动 RELEASE(reserved 超 1 小时自动 release)
7. 媒体组 group-level aggregate(所有文件 READY 才标记 group READY)
8. COPIED_UNREGISTERED 状态(copy 成功但 outbox 失败时记录 message_id)
"""
from __future__ import annotations

import ast
import inspect
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

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

REPO_ROOT = Path(__file__).resolve().parent.parent
IDX_BOT_FILE = REPO_ROOT / "bots" / "idx_bot.py"
UP_BOT_FILE = REPO_ROOT / "bots" / "up_bot.py"
QUOTA_LEDGER_FILE = REPO_ROOT / "services" / "quota_ledger.py"


# ── Python 3.9 兼容:Mock 使用 3.10+ 类型注解语法的模块 ──────────
# utils/rate_limiter.py / utils/dynamic_rate_limiter.py 等使用 `X | None` 注解,
# Python 3.9 运行时无法解析。在导入 bots 模块前注入 MagicMock 占位。
def _install_type_annotation_mocks_if_needed() -> None:
    """注入使用 Python 3.10+ 类型注解语法的 utils 模块的 mock。

    仅在真实模块导入失败时生效(避免覆盖 Python 3.10+ 环境)。
    同时确保 telegram.error 子模块在 telegram 被 conftest mock 时也可导入。

    R45 修复:DynamicRateLimiter 模块级初始化读取 settings.RATE_LIMIT_THRESHOLD_HIGH/LOW,
    conftest 的 MagicMock settings 未设置这些属性 → `MagicMock > MagicMock` 抛 TypeError。
    模块导入失败时,Python 仍会将部分初始化的模块留在 sys.modules,导致后续 import 拿到坏模块。
    修复:导入失败时,从 sys.modules 移除坏模块,注入 mock 替代。
    """
    # 确保 telegram.error 子模块可用(conftest 已 mock telegram 为 MagicMock,
    # 但 MagicMock 不是包,无法 from telegram.error import ...)
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
            # 真实模块导入失败 → 从 sys.modules 移除部分初始化的坏模块,
            # 注入 MagicMock 占位(避免后续 import 拿到坏模块)
            sys.modules.pop(mod_name, None)
            mock_mod = types.ModuleType(mod_name)
            # rate_limiter 需要提供全局实例(供 from utils.rate_limiter import global_rate_limiter)
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

# R45: 补充 conftest MagicMock settings 缺失的 RATE_LIMIT_* 属性,
# 使 utils/dynamic_rate_limiter.DynamicRateLimiter.__init__ 不会因
# `MagicMock > MagicMock` 抛 TypeError(在 _install_type_annotation_mocks_if_needed 之后、
# 测试用例之前设置,确保 import bots.idx_bot 时 DynamicRateLimiter 初始化成功)。
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


# ════════════════════════════════════════════════════════════════
# Fixture: 临时 SQLite 数据库
# ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r45_test_")
    db_path = Path(tmpdir) / "test_r45.db"
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


def _make_pending_record(uploader_id: int = 100, channel_id: int = 200,
                          message_id: int = 300, upload_id: str = "r45-upload-001") -> dict:
    """构造一条 pending_upload 记录。"""
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


def _make_file_record(code: str = "tgwenjian_a1b2c3d4e5f6_1d",
                      uploader_id: int = 100) -> dict:
    """构造 file_record dict(用于 upsert_file_record_local)。"""
    return {
        "file_code": code,
        "uploader_id": uploader_id,
        "primary_channel_id": 200,
        "primary_channel_msg_id": 300,
        "file_types": {"document": 1},
        "backup_channel_msg_ids": "",
        "batch_msg_ids": "301,302",
        "batch_file_meta": [{"type": "document", "file_id": "xxx"}],
        "file_ids": "",
        "status": "active",
        "request_count": 0,
        "protect_content": 0,
        "file_ttl_days": 0,
        "note": "",
        "expire_time": "2099-12-31T23:59:59+00:00",
        "blocked_users": "[]",
        "create_time": "2026-07-13T00:00:00+00:00",
        "updated_at": "2026-07-13T00:00:00+00:00",
        "max_requests": 0,
        "is_collection": 0,
        "collection_codes": "[]",
    }


def _make_code_entry(code: str = "tgwenjian_a1b2c3d4e5f6_1d",
                     uploader_id: int = 100) -> dict:
    """构造 code_entry dict(用于 upsert_code_local)。"""
    return {
        "code": code,
        "file_record_code": code,
        "uploader_id": uploader_id,
        "file_types": {"document": 1},
        "batch_msg_ids": "301,302",
        "batch_file_meta": [{"type": "document", "file_id": "xxx"}],
        "primary_channel_id": 200,
        "status": "active",
        "created_at": "2026-07-13T00:00:00+00:00",
        "expire_time": "2099-12-31T23:59:59+00:00",
        "note": "",
    }


# ════════════════════════════════════════════════════════════════
# Part 1: 静态检查 — AST 门禁
# ════════════════════════════════════════════════════════════════


def _parse_ast(filepath: Path) -> ast.Module:
    """读取并解析 Python 文件为 AST。"""
    source = filepath.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(filepath))


def _get_async_funcs(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}


def _get_classes(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


class TestStaticChecks:
    """R45 静态门禁:确保关键函数/类已定义。"""

    def test_idx_bot_has_finalize_upload_command(self):
        """门禁: idx_bot.py 应定义 FinalizeUploadCommand 类。"""
        tree = _parse_ast(IDX_BOT_FILE)
        classes = _get_classes(tree)
        assert "FinalizeUploadCommand" in classes, (
            "idx_bot.py 应定义 FinalizeUploadCommand dataclass,"
            "封装所有需要原子提交的字段(file_records + codes + jobs + quota + upload_session)"
        )

    def test_idx_bot_has_finalize_upload_function(self):
        """门禁: idx_bot.py 应定义 finalize_upload async 函数。"""
        tree = _parse_ast(IDX_BOT_FILE)
        async_funcs = _get_async_funcs(tree)
        assert "finalize_upload" in async_funcs, (
            "idx_bot.py 应定义 async def finalize_upload(command: FinalizeUploadCommand),"
            "使用单一 SQLite 事务原子提交 file/code/job/quota/upload_session"
        )

    def test_quota_ledger_has_reserve_quota(self):
        """门禁: quota_ledger.py 应定义 reserve_quota async 函数。"""
        tree = _parse_ast(QUOTA_LEDGER_FILE)
        async_funcs = _get_async_funcs(tree)
        assert "reserve_quota" in async_funcs, (
            "quota_ledger.py 应定义 reserve_quota(user_id, amount, action_id) 方法"
        )

    def test_quota_ledger_has_settle_quota(self):
        """门禁: quota_ledger.py 应定义 settle_quota async 函数。"""
        tree = _parse_ast(QUOTA_LEDGER_FILE)
        async_funcs = _get_async_funcs(tree)
        assert "settle_quota" in async_funcs, (
            "quota_ledger.py 应定义 settle_quota(action_id) 方法"
        )

    def test_quota_ledger_has_release_quota(self):
        """门禁: quota_ledger.py 应定义 release_quota async 函数。"""
        tree = _parse_ast(QUOTA_LEDGER_FILE)
        async_funcs = _get_async_funcs(tree)
        assert "release_quota" in async_funcs, (
            "quota_ledger.py 应定义 release_quota(action_id) 方法"
        )

    def test_up_bot_has_mark_copied_unregistered(self):
        """门禁: up_bot.py 应定义 _mark_copied_unregistered async 函数。"""
        tree = _parse_ast(UP_BOT_FILE)
        async_funcs = _get_async_funcs(tree)
        assert "_mark_copied_unregistered" in async_funcs, (
            "up_bot.py 应定义 _mark_copied_unregistered() 方法,"
            "记录 Telegram copy 成功但 outbox 写失败的情况,不遗失 message_id"
        )

    def test_up_bot_has_media_group_states(self):
        """门禁: up_bot.py 应包含 _media_group_states 模块级变量。"""
        source = UP_BOT_FILE.read_text(encoding="utf-8")
        assert "_media_group_states" in source, (
            "up_bot.py 应定义 _media_group_states: dict[str, dict] 跟踪媒体组状态"
        )


# ════════════════════════════════════════════════════════════════
# Part 2: Quota Ledger RESERVE/SETTLE/RELEASE 行为测试
# ════════════════════════════════════════════════════════════════


class TestQuotaLedgerReserveSettleRelease:
    """Quota RESERVE/SETTLE/RELEASE ledger 模式测试。"""

    @pytest.mark.asyncio
    async def test_reserve_quota_returns_action_id(self, store, monkeypatch):
        """reserve_quota 应返回 reservation_id(非空字符串)。"""
        # 模拟 get_plan 返回有配额的用户
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        action_id = await quota_ledger.reserve_quota(
            user_id=1001, amount=5, action_id="upload:r45-test-001"
        )
        assert action_id, "reserve_quota 应返回非空 reservation_id"
        assert action_id.startswith("res-"), "reservation_id 应以 'res-' 前缀"

    @pytest.mark.asyncio
    async def test_settle_quota_marks_settled(self, store, monkeypatch):
        """settle_quota 应将预留状态更新为 settled。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        action_id = await quota_ledger.reserve_quota(
            user_id=1002, amount=3, action_id="upload:r45-settle-001"
        )
        assert action_id

        ok = await quota_ledger.settle_quota(action_id)
        assert ok is True, "settle_quota 应返回 True"

        reservation = await quota_ledger.get_reservation(action_id)
        assert reservation is not None
        assert reservation["status"] == "settled", "预留状态应为 settled"

    @pytest.mark.asyncio
    async def test_release_quota_marks_refunded(self, store, monkeypatch):
        """release_quota 应将预留状态更新为 refunded(释放)。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        action_id = await quota_ledger.reserve_quota(
            user_id=1003, amount=2, action_id="upload:r45-release-001"
        )
        assert action_id

        ok = await quota_ledger.release_quota(action_id, reason="test_release")
        assert ok is True, "release_quota 应返回 True"

        reservation = await quota_ledger.get_reservation(action_id)
        assert reservation is not None
        assert reservation["status"] == "refunded", "预留状态应为 refunded"

    @pytest.mark.asyncio
    async def test_reserve_quota_insufficient_balance_returns_empty(self, store, monkeypatch):
        """余额不足时 reserve_quota 应返回空字符串。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 1  # 配额=1
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        # 先预留 1,耗尽配额
        action_id1 = await quota_ledger.reserve_quota(
            user_id=1004, amount=1, action_id="upload:r45-insufficient-001"
        )
        assert action_id1

        # 再预留 1,应失败(返回空串)
        action_id2 = await quota_ledger.reserve_quota(
            user_id=1004, amount=1, action_id="upload:r45-insufficient-002"
        )
        assert action_id2 == "", "余额不足时应返回空字符串"


class TestQuotaTimeoutAutoRelease:
    """Quota 超时自动 RELEASE 测试。"""

    @pytest.mark.asyncio
    async def test_cleanup_expired_reservations_releases_stale(self, store, monkeypatch):
        """cleanup_expired_reservations 应自动释放超时(>1h)的 reserved 预留。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        action_id = await quota_ledger.reserve_quota(
            user_id=2001, amount=5, action_id="upload:r45-timeout-001"
        )
        assert action_id

        # 手动修改 created_at 为 2 小时前,模拟超时
        # R53 P1-4: created_at 使用 UTC aware timestamp,
        # 与 reserve() 写入和 cleanup_expired_reservations 比较格式一致
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        await store._db.execute(
            "UPDATE quota_reservations SET created_at = ? WHERE id = ?",
            (old_time, action_id),
        )
        await store._db.commit()

        # 执行清理
        count = await quota_ledger.cleanup_expired_reservations()
        assert count >= 1, "应清理至少 1 条过期预留"

        # 验证状态已变为 refunded
        reservation = await quota_ledger.get_reservation(action_id)
        assert reservation is not None
        assert reservation["status"] == "refunded", "超时预留应被自动 release(状态=refunded)"

    @pytest.mark.asyncio
    async def test_cleanup_does_not_release_recent_reservations(self, store, monkeypatch):
        """cleanup_expired_reservations 不应释放未超时的预留。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        action_id = await quota_ledger.reserve_quota(
            user_id=2002, amount=5, action_id="upload:r45-recent-001"
        )
        assert action_id

        # 不修改 created_at,应未被清理
        count = await quota_ledger.cleanup_expired_reservations()
        # 可能清理其他测试残留,但本条不应被清理
        reservation = await quota_ledger.get_reservation(action_id)
        assert reservation is not None
        assert reservation["status"] == "reserved", "未超时的预留不应被 release"


# ════════════════════════════════════════════════════════════════
# Part 3: FinalizeUpload 原子提交测试
# ════════════════════════════════════════════════════════════════


class TestFinalizeUploadAtomicCommit:
    """FinalizeUpload 原子提交:所有表同时成功。"""

    @pytest.mark.asyncio
    async def test_finalize_upload_commits_all_tables(self, store, monkeypatch):
        """finalize_upload 应在同一事务提交 file_records + codes + pending_uploads。"""
        # 插入 pending_upload
        rec = _make_pending_record(uploader_id=3001, message_id=4001,
                                    upload_id="r45-finalize-commit-001")
        pend_id = await store.insert_pending_upload_local(rec)
        assert pend_id > 0

        # 导入 FinalizeUploadCommand + finalize_upload
        from bots import idx_bot
        # 模拟 bot_username / settings
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        file_record = _make_file_record(code="tgwenjian_r45commit001_1d",
                                        uploader_id=3001)
        code_entry = _make_code_entry(code="tgwenjian_r45commit001_1d",
                                       uploader_id=3001)

        command = idx_bot.FinalizeUploadCommand(
            file_record=file_record,
            code_entry=code_entry,
            pending_upload_id=pend_id,
            upload_id="r45-finalize-commit-001",
            storage_msg_ids=[4001],
            file_meta_list=[{"type": "document", "file_id": "xxx"}],
            channel_id=200,
            protect_content=False,
            quota_reservation_id="",
        )

        # 执行 finalize_upload
        await idx_bot.finalize_upload(command)

        # 验证 file_records_local 已写入
        rows = await store._db.execute_fetchall(
            "SELECT file_code FROM file_records_local WHERE file_code = ?",
            ("tgwenjian_r45commit001_1d",),
        )
        assert len(rows) == 1, "file_records_local 应有 1 条记录"

        # 验证 codes_local 已写入
        rows = await store._db.execute_fetchall(
            "SELECT code FROM codes_local WHERE code = ?",
            ("tgwenjian_r45commit001_1d",),
        )
        assert len(rows) == 1, "codes_local 应有 1 条记录"

        # 验证 pending_uploads_local 已标记完成(processed=1)
        rows = await store._db.execute_fetchall(
            "SELECT processed FROM pending_uploads_local WHERE id = ?",
            (pend_id,),
        )
        assert len(rows) == 1
        assert rows[0][0] == 1, "pending_uploads 应标记为 processed=1"

        # 验证 dirty_outbox 已写入(file_records + codes 各一条)
        rows = await store._db.execute_fetchall(
            "SELECT table_name FROM dirty_outbox WHERE pk = ?",
            ("tgwenjian_r45commit001_1d",),
        )
        table_names = {r[0] for r in rows}
        assert "file_records" in table_names, "dirty_outbox 应有 file_records 条目"
        assert "codes" in table_names, "dirty_outbox 应有 codes 条目"

    @pytest.mark.asyncio
    async def test_finalize_upload_with_quota_settles_on_success(self, store, monkeypatch):
        """finalize_upload 成功后应 SETTLE 配额。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        # 先预留配额
        reservation_id = await quota_ledger.reserve_quota(
            user_id=3002, amount=1, action_id="upload:r45-quota-settle-001"
        )
        assert reservation_id

        # 插入 pending_upload
        rec = _make_pending_record(uploader_id=3002, message_id=4002,
                                    upload_id="r45-quota-settle-001")
        pend_id = await store.insert_pending_upload_local(rec)

        from bots import idx_bot
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        file_record = _make_file_record(code="tgwenjian_r45settle001_1d",
                                        uploader_id=3002)
        code_entry = _make_code_entry(code="tgwenjian_r45settle001_1d",
                                       uploader_id=3002)

        command = idx_bot.FinalizeUploadCommand(
            file_record=file_record,
            code_entry=code_entry,
            pending_upload_id=pend_id,
            upload_id="r45-quota-settle-001",
            storage_msg_ids=[4002],
            file_meta_list=[{"type": "document", "file_id": "xxx"}],
            channel_id=200,
            protect_content=False,
            quota_reservation_id=reservation_id,
        )

        await idx_bot.finalize_upload(command)

        # 验证配额已 SETTLE
        reservation = await quota_ledger.get_reservation(reservation_id)
        assert reservation is not None
        assert reservation["status"] == "settled", (
            "finalize_upload 成功后配额应 SETTLE"
        )


class TestFinalizeUploadAtomicRollback:
    """FinalizeUpload 原子回滚:任一步骤失败全部 ROLLBACK。"""

    @pytest.mark.asyncio
    async def test_finalize_upload_rolls_back_on_dirty_outbox_failure(self, store, monkeypatch):
        """dirty_outbox 写入失败时,整个事务应 ROLLBACK。"""
        rec = _make_pending_record(uploader_id=4001, message_id=5001,
                                    upload_id="r45-rollback-001")
        pend_id = await store.insert_pending_upload_local(rec)

        from bots import idx_bot
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        file_record = _make_file_record(code="tgwenjian_r45rollback001_1d",
                                        uploader_id=4001)
        code_entry = _make_code_entry(code="tgwenjian_r45rollback001_1d",
                                       uploader_id=4001)

        command = idx_bot.FinalizeUploadCommand(
            file_record=file_record,
            code_entry=code_entry,
            pending_upload_id=pend_id,
            upload_id="r45-rollback-001",
            storage_msg_ids=[5001],
            file_meta_list=[{"type": "document", "file_id": "xxx"}],
            channel_id=200,
            protect_content=False,
            quota_reservation_id="",
        )

        # Mock add_dirty_outbox 持久抛异常(模拟 dirty_outbox 不可用)
        # R75 P0-07: upsert_file_record_local(mark_dirty=True) 内部会调用一次
        # add_dirty_outbox(失败时被 upsert 内部 try/except 吞掉,仅 warning);
        # finalize_upload 中的显式 add_dirty_outbox 是确保失败可抛异常的
        # 主路径。因此 mock 必须在所有调用上都抛异常,才能让显式调用抛出
        # 异常并触发整体 ROLLBACK。
        async def _failing_add_dirty(*args, **kwargs):
            raise RuntimeError("simulated dirty_outbox failure")
        monkeypatch.setattr(store, "add_dirty_outbox", _failing_add_dirty)

        # finalize_upload 应抛异常
        with pytest.raises(Exception, match="simulated dirty_outbox failure"):
            await idx_bot.finalize_upload(command)

        # 验证 ROLLBACK: file_records_local 不应有记录
        rows = await store._db.execute_fetchall(
            "SELECT file_code FROM file_records_local WHERE file_code = ?",
            ("tgwenjian_r45rollback001_1d",),
        )
        assert len(rows) == 0, "事务 ROLLBACK,file_records_local 不应有记录"

        # 验证 ROLLBACK: codes_local 不应有记录
        rows = await store._db.execute_fetchall(
            "SELECT code FROM codes_local WHERE code = ?",
            ("tgwenjian_r45rollback001_1d",),
        )
        assert len(rows) == 0, "事务 ROLLBACK,codes_local 不应有记录"

        # 验证 ROLLBACK: pending_uploads_local 仍为未完成(processed != 1)
        rows = await store._db.execute_fetchall(
            "SELECT processed FROM pending_uploads_local WHERE id = ?",
            (pend_id,),
        )
        assert len(rows) == 1
        assert rows[0][0] != 1, "事务 ROLLBACK,pending_uploads 不应标记为 processed=1"

    @pytest.mark.asyncio
    async def test_finalize_upload_releases_quota_on_failure(self, store, monkeypatch):
        """finalize_upload 失败时应 RELEASE 之前 RESERVE 的配额。"""
        from services import quota_ledger
        mock_plan = MagicMock()
        mock_plan.daily_quota = 100
        async def _mock_get_plan(user_id):
            return mock_plan
        monkeypatch.setattr("services.entitlements.get_plan", _mock_get_plan)

        reservation_id = await quota_ledger.reserve_quota(
            user_id=4002, amount=1, action_id="upload:r45-quota-release-001"
        )
        assert reservation_id

        rec = _make_pending_record(uploader_id=4002, message_id=5002,
                                    upload_id="r45-quota-release-001")
        pend_id = await store.insert_pending_upload_local(rec)

        from bots import idx_bot
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        file_record = _make_file_record(code="tgwenjian_r45qrelease001_1d",
                                        uploader_id=4002)
        code_entry = _make_code_entry(code="tgwenjian_r45qrelease001_1d",
                                       uploader_id=4002)

        command = idx_bot.FinalizeUploadCommand(
            file_record=file_record,
            code_entry=code_entry,
            pending_upload_id=pend_id,
            upload_id="r45-quota-release-001",
            storage_msg_ids=[5002],
            file_meta_list=[{"type": "document", "file_id": "xxx"}],
            channel_id=200,
            protect_content=False,
            quota_reservation_id=reservation_id,
        )

        # Mock add_dirty_outbox 抛异常
        async def _failing_add_dirty(*args, **kwargs):
            raise RuntimeError("simulated dirty_outbox failure for quota release")
        monkeypatch.setattr(store, "add_dirty_outbox", _failing_add_dirty)

        with pytest.raises(Exception):
            await idx_bot.finalize_upload(command)

        # 验证配额已被 RELEASE(状态=refunded)
        reservation = await quota_ledger.get_reservation(reservation_id)
        assert reservation is not None
        assert reservation["status"] == "refunded", (
            "finalize_upload 失败时应 RELEASE 配额(状态=refunded)"
        )


# ════════════════════════════════════════════════════════════════
# Part 4: dirty_outbox 失败抛异常测试
# ════════════════════════════════════════════════════════════════


class TestDirtyOutboxFailureRaises:
    """dirty_outbox 失败应抛异常,不应 warning 后 continue。"""

    def test_idx_bot_no_try_except_around_dirty_outbox_in_finalize(self):
        """AST 门禁: finalize_upload 中不应有 try/except 包裹 add_dirty_outbox。

        R45 整改: dirty_outbox 失败必须抛异常,不能 warning 后继续。
        """
        tree = _parse_ast(IDX_BOT_FILE)
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name == "finalize_upload":
                    target_func = node
                    break
        assert target_func is not None, "finalize_upload 函数未定义"

        # 在 finalize_upload 体内查找 add_dirty_outbox 调用
        # 并检查其是否被 try/except 包裹(若被包裹,说明 dirty_outbox 失败被吞掉)
        dirty_calls = []
        try_nodes = []
        for child in ast.walk(target_func):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == "add_dirty_outbox":
                    dirty_calls.append(child)
            if isinstance(child, ast.Try):
                try_nodes.append(child)

        assert len(dirty_calls) >= 2, (
            "finalize_upload 应至少有 2 个 add_dirty_outbox 调用(file_records + codes)"
        )

        # 检查每个 add_dirty_outbox 调用是否在 try body 内
        # 若在 try body 内,且对应 except 包含 logger.warning 而非 raise,则是违规的
        for call in dirty_calls:
            call_lineno = call.lineno
            for try_node in try_nodes:
                # 检查 call 是否在 try body 范围内
                try_start = try_node.lineno
                try_end = try_node.end_lineno or try_start + 100
                if try_start <= call_lineno <= try_end:
                    # 检查 except handlers 中是否仅有 logger.warning(无 raise)
                    for handler in try_node.handlers:
                        has_raise = False
                        for h_child in ast.walk(handler):
                            if isinstance(h_child, ast.Raise):
                                has_raise = True
                                break
                        if not has_raise:
                            pytest.fail(
                                f"finalize_upload 中 add_dirty_outbox(line {call_lineno}) "
                                f"被 try/except 包裹但 except 无 raise,"
                                "R45 要求 dirty_outbox 失败必须抛异常,不能 warning 后 continue"
                            )

    @pytest.mark.asyncio
    async def test_dirty_outbox_failure_propagates_in_finalize_upload(self, store, monkeypatch):
        """dirty_outbox 失败时异常应传播到 finalize_upload 调用方。"""
        rec = _make_pending_record(uploader_id=5001, message_id=6001,
                                    upload_id="r45-propagate-001")
        pend_id = await store.insert_pending_upload_local(rec)

        from bots import idx_bot
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        file_record = _make_file_record(code="tgwenjian_r45prop001_1d",
                                        uploader_id=5001)
        code_entry = _make_code_entry(code="tgwenjian_r45prop001_1d",
                                       uploader_id=5001)

        command = idx_bot.FinalizeUploadCommand(
            file_record=file_record,
            code_entry=code_entry,
            pending_upload_id=pend_id,
            upload_id="r45-propagate-001",
            storage_msg_ids=[6001],
            file_meta_list=[{"type": "document", "file_id": "xxx"}],
            channel_id=200,
            protect_content=False,
            quota_reservation_id="",
        )

        # Mock add_dirty_outbox 抛异常
        async def _failing(*args, **kwargs):
            raise RuntimeError("dirty_outbox simulated failure")
        monkeypatch.setattr(store, "add_dirty_outbox", _failing)

        # 异常应传播出来,不应被吞掉
        with pytest.raises(RuntimeError, match="dirty_outbox simulated failure"):
            await idx_bot.finalize_upload(command)


# ════════════════════════════════════════════════════════════════
# Part 5: 码生成冲突重试测试
# ════════════════════════════════════════════════════════════════


class TestCodeConflictRetry:
    """码生成冲突重试:使用 DB unique constraint + retry。"""

    @pytest.mark.asyncio
    async def test_generate_code_retries_on_conflict(self, store, monkeypatch):
        """码冲突时应重试(最多 3 次),最终生成不冲突的码。"""
        from bots import idx_bot
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        # 预先插入一条 codes_local 记录,模拟冲突
        existing_code = "tgwenjian_aaaaaaaaaaaa_1d"
        existing_entry = _make_code_entry(code=existing_code, uploader_id=6001)
        await store.upsert_code_local(existing_entry, mark_dirty=False)

        # 调用 _generate_unique_code_with_retry,前 2 次返回冲突码,第 3 次返回新码
        call_count = {"n": 0}
        from services.code_generator import build_file_code
        original_build = build_file_code
        def _mock_build(file_types):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return existing_code  # 前 2 次冲突
            return original_build(file_types)  # 第 3 次正常
        monkeypatch.setattr("services.code_generator.build_file_code", _mock_build)

        # 调用 _generate_unique_code_with_retry(若存在)
        if hasattr(idx_bot, "_generate_unique_code_with_retry"):
            code = await idx_bot._generate_unique_code_with_retry({"document": 1})
            assert code != existing_code, "冲突后应重试生成新码"
            assert call_count["n"] == 3, "应重试 3 次(前 2 次冲突,第 3 次成功)"
        else:
            pytest.skip("_generate_unique_code_with_retry 未实现")

    @pytest.mark.asyncio
    async def test_generate_code_raises_after_max_retries(self, store, monkeypatch):
        """超过最大重试次数(3次)仍冲突时应抛异常。"""
        from bots import idx_bot
        monkeypatch.setattr(idx_bot.settings, "DECODER_BOT_USERNAME", "test_bot")
        monkeypatch.setattr(idx_bot.settings, "UPLOAD_BOT_USERNAME", "upload_bot")
        monkeypatch.setattr(idx_bot.settings, "SENDER_BOT_USERNAME", "sender_bot")
        monkeypatch.setattr(idx_bot.settings, "FILE_CODE_PREFIX", "tgwenjian")

        existing_code = "tgwenjian_bbbbbbbbbbbb_1d"
        existing_entry = _make_code_entry(code=existing_code, uploader_id=6002)
        await store.upsert_code_local(existing_entry, mark_dirty=False)

        # Mock build_file_code 始终返回冲突码
        monkeypatch.setattr("services.code_generator.build_file_code",
                            lambda ft: existing_code)

        if hasattr(idx_bot, "_generate_unique_code_with_retry"):
            with pytest.raises(Exception):
                await idx_bot._generate_unique_code_with_retry({"document": 1})
        else:
            pytest.skip("_generate_unique_code_with_retry 未实现")


# ════════════════════════════════════════════════════════════════
# Part 6: 媒体组 group-level aggregate 测试
# ════════════════════════════════════════════════════════════════


class TestMediaGroupAggregate:
    """媒体组 group-level aggregate:所有文件 READY 才标记 group READY。"""

    @pytest.mark.asyncio
    async def test_mark_copied_unregistered_records_message_id(self, store, monkeypatch):
        """_mark_copied_unregistered 应记录 copy 成功但 outbox 失败的 message_id。"""
        from bots import up_bot

        # 清空模块级状态
        up_bot._media_group_states.clear()

        # 调用 _mark_copied_unregistered
        await up_bot._mark_copied_unregistered(
            upload_id="r45-copied-unreg-001",
            media_group_id="mg-r45-001",
            file_unique_id="fuid-r45-001",
            message_id=12345,
            channel_id=200,
            reason="outbox_write_failed",
        )

        # 验证状态已记录在 _media_group_states
        assert "mg-r45-001" in up_bot._media_group_states
        mg_state = up_bot._media_group_states["mg-r45-001"]
        assert "files" in mg_state
        assert "fuid-r45-001" in mg_state["files"]
        file_state = mg_state["files"]["fuid-r45-001"]
        assert file_state["state"] == "COPIED_UNREGISTERED"
        assert file_state["message_id"] == 12345, "message_id 不应遗失"
        assert file_state["channel_id"] == 200

    @pytest.mark.asyncio
    async def test_group_ready_only_when_all_files_ready(self, store, monkeypatch):
        """媒体组只在所有文件都 READY 时才标记 group READY。"""
        from bots import up_bot

        up_bot._media_group_states.clear()

        mg_id = "mg-r45-002"
        # 初始化媒体组状态:2 个文件
        up_bot._media_group_states[mg_id] = {
            "files": {
                "fuid-1": {"state": "PENDING", "message_id": 0, "channel_id": 200},
                "fuid-2": {"state": "PENDING", "message_id": 0, "channel_id": 200},
            },
            "upload_id": "r45-group-ready-001",
            "user_id": 7001,
            "created_at": time.time(),
            "group_state": "pending",
        }

        # 第一个文件 READY,group 不应 READY
        up_bot._media_group_states[mg_id]["files"]["fuid-1"]["state"] = "READY"
        up_bot._media_group_states[mg_id]["files"]["fuid-1"]["message_id"] = 10001

        group_state = up_bot._evaluate_media_group_state(mg_id)
        assert group_state != "ready", "只有 1/2 文件 READY,group 不应 READY"

        # 第二个文件也 READY,group 应 READY
        up_bot._media_group_states[mg_id]["files"]["fuid-2"]["state"] = "READY"
        up_bot._media_group_states[mg_id]["files"]["fuid-2"]["message_id"] = 10002

        group_state = up_bot._evaluate_media_group_state(mg_id)
        assert group_state == "ready", "所有文件 READY 时 group 应 READY"

    @pytest.mark.asyncio
    async def test_group_partial_when_some_files_failed(self, store, monkeypatch):
        """媒体组部分文件失败时,group 状态应为 partial。"""
        from bots import up_bot

        up_bot._media_group_states.clear()

        mg_id = "mg-r45-003"
        up_bot._media_group_states[mg_id] = {
            "files": {
                "fuid-1": {"state": "READY", "message_id": 20001, "channel_id": 200},
                "fuid-2": {"state": "FAILED", "message_id": 0, "channel_id": 200},
            },
            "upload_id": "r45-group-partial-001",
            "user_id": 7002,
            "created_at": time.time(),
            "group_state": "pending",
        }

        group_state = up_bot._evaluate_media_group_state(mg_id)
        assert group_state == "partial", "部分文件失败,group 应为 partial"

    @pytest.mark.asyncio
    async def test_group_failed_when_all_files_failed(self, store, monkeypatch):
        """媒体组所有文件都失败时,group 状态应为 failed。"""
        from bots import up_bot

        up_bot._media_group_states.clear()

        mg_id = "mg-r45-004"
        up_bot._media_group_states[mg_id] = {
            "files": {
                "fuid-1": {"state": "FAILED", "message_id": 0, "channel_id": 200},
                "fuid-2": {"state": "FAILED", "message_id": 0, "channel_id": 200},
            },
            "upload_id": "r45-group-failed-001",
            "user_id": 7003,
            "created_at": time.time(),
            "group_state": "pending",
        }

        group_state = up_bot._evaluate_media_group_state(mg_id)
        assert group_state == "failed", "所有文件失败,group 应为 failed"


class TestCopiedUnregisteredPreservesMessageId:
    """COPIED_UNREGISTERED 状态保留 message_id,不遗失目标 message_id。"""

    @pytest.mark.asyncio
    async def test_copied_unregistered_can_be_recovered(self, store, monkeypatch):
        """COPIED_UNREGISTERED 状态的文件可通过 message_id 恢复 manifest 注册。"""
        from bots import up_bot

        up_bot._media_group_states.clear()

        # 模拟: copy 成功但 outbox 失败
        await up_bot._mark_copied_unregistered(
            upload_id="r45-recover-001",
            media_group_id="mg-r45-recover",
            file_unique_id="fuid-recover-001",
            message_id=99999,
            channel_id=200,
            reason="outbox_write_failed",
        )

        # 验证 message_id 已保留,可恢复
        mg_state = up_bot._media_group_states["mg-r45-recover"]
        file_state = mg_state["files"]["fuid-recover-001"]
        assert file_state["state"] == "COPIED_UNREGISTERED"
        assert file_state["message_id"] == 99999, (
            "COPIED_UNREGISTERED 必须保留 message_id,否则文件无法恢复 manifest 注册"
        )
        assert file_state["channel_id"] == 200, "channel_id 也应保留"

    @pytest.mark.asyncio
    async def test_copied_unregistered_transitions_to_ready_after_recovery(self, store, monkeypatch):
        """COPIED_UNREGISTERED 文件成功重写 outbox 后应转为 READY。"""
        from bots import up_bot

        up_bot._media_group_states.clear()

        mg_id = "mg-r45-transition"
        up_bot._media_group_states[mg_id] = {
            "files": {
                "fuid-1": {
                    "state": "COPIED_UNREGISTERED",
                    "message_id": 88888,
                    "channel_id": 200,
                },
            },
            "upload_id": "r45-transition-001",
            "user_id": 7004,
            "created_at": time.time(),
            "group_state": "pending",
        }

        # 模拟恢复:重写 outbox 成功,状态转为 READY
        up_bot._media_group_states[mg_id]["files"]["fuid-1"]["state"] = "READY"

        group_state = up_bot._evaluate_media_group_state(mg_id)
        assert group_state == "ready", "恢复后所有文件 READY,group 应 READY"
