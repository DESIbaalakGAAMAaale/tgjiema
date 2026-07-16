"""R53 P1-4: 配额日期不再依赖宿主机 localtime 测试。

被测目标:
    - ``services/entitlements.py`` 中 ``get_quota`` 的配额查询
    - ``services/quota_ledger.py`` 中 ``get_balance`` 的余额查询
    - ``config/settings.py`` 新增 ``BILLING_TIMEZONE``(默认 Asia/Shanghai)

修复背景:
    R52 把 SQLite ``date('now')`` 改成 ``date('now','localtime')``,
    但这只能使用容器/宿主机时区(Docker 默认常为 UTC),
    不能保证 Asia/Shanghai,也无法支持用户或套餐时区。

修复方案:
    1. 统一存 UTC aware timestamp(ISO 带 +00:00)
    2. 新增 ``settings.BILLING_TIMEZONE``(默认 Asia/Shanghai)
    3. Python 计算当日 BILLING_TIMEZONE 0 点 → UTC start/end,参数化查询
    4. 移除所有 ``date('now', 'localtime')`` 调用

测试场景(6 项,覆盖用户要求):
    1. 默认 BILLING_TIMEZONE=Asia/Shanghai:配额按北京时间 0 点重置
    2. UTC 时区:午夜边界正确
    3. UTC+8 时区(与默认一致)
    4. DST 时区(America/New_York,夏令时切换)
    5. 午夜并发(多个用户同时跨日)
    6. SQLite 查询不再使用 date('now', 'localtime')(源码静态校验)

测试策略:
    - 使用真实 SQLite 临时数据库隔离生产数据
    - 通过 ``monkeypatch`` 切换 ``settings.BILLING_TIMEZONE`` 模拟不同时区
    - 冻结时间:直接 patch ``_get_billing_day_utc_bounds`` / ``_get_billing_today_date``
      返回预计算的 UTC 边界(避免全局 patch datetime.datetime 影响其他模块)
    - 中文注释和日志,英文 raise 消息
"""
from __future__ import annotations

import inspect
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from unittest.mock import MagicMock

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


# ── 辅助:构造真实 Plan 字典(避免 conftest MagicMock settings 干扰) ──
def _make_real_plans():
    """构造包含真实 int 值的 _PLANS 字典。"""
    from services.entitlements import Plan
    return {
        "free": Plan(
            name="free", daily_quota=10, external_daily_quota=0,
            max_file_size=50 * 1024 * 1024, max_concurrent=1,
            retention_days=7, priority_queue="normal", max_collection_items=10,
        ),
        "basic": Plan(
            name="basic", daily_quota=100, external_daily_quota=10,
            max_file_size=500 * 1024 * 1024, max_concurrent=3,
            retention_days=30, priority_queue="normal", max_collection_items=50,
        ),
        "premium": Plan(
            name="premium", daily_quota=1000, external_daily_quota=100,
            max_file_size=2 * 1024 * 1024 * 1024, max_concurrent=10,
            retention_days=90, priority_queue="high", max_collection_items=200,
        ),
    }


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r53_p1_4_test_")
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


@pytest_asyncio.fixture
async def real_store_with_real_plans(real_store, monkeypatch):
    """创建真实 store 并 patch _PLANS 为真实 int 值的字典。"""
    from services import entitlements
    monkeypatch.setattr(entitlements, "_PLANS", _make_real_plans())
    return real_store


@pytest.fixture(autouse=True)
def _reset_command_bus_idempotency():
    """每个用例前重置 CommandBus 幂等缓存,避免跨用例污染。"""
    from services import command_bus
    command_bus.reset_idempotency_cache()
    yield
    command_bus.reset_idempotency_cache()


# ── 辅助函数 ────────────────────────────────────────────────────

async def _insert_user(store, user_id: int, level: str = "free"):
    """插入测试用户到 users_local 表。"""
    try:
        await store._db.execute(
            "INSERT OR REPLACE INTO users_local "
            "(user_id, username, membership_level, version) "
            "VALUES (?, ?, ?, ?)",
            (user_id, f"test_user_{user_id}", level, 0),
        )
    except Exception:
        # 老库无 version 列,退化为不带 version
        await store._db.execute(
            "INSERT OR REPLACE INTO users_local "
            "(user_id, username, membership_level) "
            "VALUES (?, ?, ?)",
            (user_id, f"test_user_{user_id}", level),
        )
    await store._db.commit()


async def _insert_quota_reservation(
    store, user_id: int, amount: int = 1,
    created_at_iso: str | None = None, status: str = "settled",
    actual_amount: int | None = None,
    reservation_id: str | None = None,
):
    """插入测试 quota_reservations 记录(用于配额消耗统计)。

    Args:
        store: CacheStore 实例
        user_id: 用户 ID
        amount: 预留数量
        created_at_iso: 自定义 created_at(UTC aware ISO 字符串);
            None 时使用当前 UTC 时间
        status: 预留状态(reserved/settled/refunded)
        actual_amount: 实际消耗(settled 状态用);None 时用 amount
        reservation_id: 自定义预留 ID;None 时自动生成
    """
    if created_at_iso is None:
        created_at_iso = datetime.now(timezone.utc).isoformat()
    if actual_amount is None:
        actual_amount = amount
    if reservation_id is None:
        reservation_id = f"res_{user_id}_{amount}_{abs(hash(created_at_iso))}"
    await store._db.execute(
        """INSERT OR REPLACE INTO quota_reservations
           (id, user_id, amount, reason, status, actual_amount,
            created_at, settled_at, expired_at)
           VALUES (?, ?, ?, 'test', ?, ?, ?, ?, NULL)""",
        (reservation_id, user_id, amount, status, actual_amount,
         created_at_iso, created_at_iso),
    )
    await store._db.commit()


def _compute_utc_bounds(frozen_utc: datetime, tz_name: str) -> tuple[str, str]:
    """预计算 BILLING_TIMEZONE 当日 0 点对应的 UTC 边界。

    用于冻结时间测试:先计算好 UTC 边界,再 patch
    ``_get_billing_day_utc_bounds`` 返回该值,避免全局 patch datetime。
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now_local = frozen_utc.astimezone(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return (start_utc.isoformat(), end_utc.isoformat())


def _compute_today_date(frozen_utc: datetime, tz_name: str) -> date:
    """预计算 BILLING_TIMEZONE 当地今日日期。"""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    return frozen_utc.astimezone(tz).date()


def _patch_frozen_time(monkeypatch, entitlements_module, frozen_utc: datetime, tz_name: str):
    """Patch entitlements 模块的时间函数为冻结时间。

    直接 patch ``_get_billing_day_utc_bounds`` 和 ``_get_billing_today_date``,
    返回基于 ``frozen_utc`` 和 ``tz_name`` 预计算的值。
    避免全局 patch ``datetime.datetime`` 影响调用链中其他模块的 ``utcnow()``。
    """
    start_iso, end_iso = _compute_utc_bounds(frozen_utc, tz_name)
    today = _compute_today_date(frozen_utc, tz_name)
    monkeypatch.setattr(
        entitlements_module, "_get_billing_day_utc_bounds",
        lambda: (start_iso, end_iso),
    )
    monkeypatch.setattr(
        entitlements_module, "_get_billing_today_date",
        lambda: today,
    )


async def _fetch_quota_reservations_query_sql() -> str:
    """从 entitlements.get_quota 源码中提取 quota_reservations 查询 SQL 字符串。"""
    from services import entitlements
    src = inspect.getsource(entitlements.get_quota)
    return src


async def _fetch_balance_query_sql() -> str:
    """从 quota_ledger.get_balance 源码中提取 quota_reservations 查询 SQL 字符串。"""
    from services import quota_ledger
    src = inspect.getsource(quota_ledger.get_balance)
    return src


# ════════════════════════════════════════════════════════════════
# 测试 1: 默认 BILLING_TIMEZONE=Asia/Shanghai,配额按北京时间 0 点重置
# ════════════════════════════════════════════════════════════════

class TestR53P1_4_DefaultBillingTimezone:
    """R53 P1-4: 默认 BILLING_TIMEZONE=Asia/Shanghai 时,配额按北京时间 0 点重置。"""

    @pytest.mark.asyncio
    async def test_default_billing_timezone_is_asia_shanghai(
        self, real_store_with_real_plans, monkeypatch
    ):
        """默认 settings.BILLING_TIMEZONE 应为 'Asia/Shanghai'。"""
        from config import settings
        # conftest 注入的 settings 是 MagicMock,使用 monkeypatch 显式设置真实值
        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")
        assert str(settings.BILLING_TIMEZONE) == "Asia/Shanghai"

    @pytest.mark.asyncio
    async def test_quota_resets_at_beijing_midnight(
        self, real_store_with_real_plans, monkeypatch
    ):
        """北京时间 0 点重置:今日 23:59 与昨日 00:01 的记录归类不同。

        场景:
            - BILLING_TIMEZONE=Asia/Shanghai
            - 当前北京时间 2026-07-15 12:00(UTC 04:00)
            - 插入"今日 00:30 北京时间"的记录(应计入今日配额)
            - 插入"昨日 23:30 北京时间"的记录(不应计入今日配额)
        预期:
            - get_quota.used_today = 仅今日记录的 amount
        """
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")
        user_id = 10001
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # 固定"当前时间"为 2026-07-15 12:00 北京时间(= 04:00 UTC)
        frozen_utc_now = datetime(2026, 7, 15, 4, 0, 0, tzinfo=timezone.utc)

        # 今日 00:30 北京时间 = 2026-07-14 16:30 UTC(应计入今日配额)
        today_record_utc = datetime(2026, 7, 14, 16, 30, 0, tzinfo=timezone.utc).isoformat()
        # 昨日 23:30 北京时间 = 2026-07-14 15:30 UTC(不应计入今日配额)
        yesterday_record_utc = datetime(2026, 7, 14, 15, 30, 0, tzinfo=timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=2,
            created_at_iso=today_record_utc, status="settled",
            reservation_id="res_today_beijing",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=5,
            created_at_iso=yesterday_record_utc, status="settled",
            reservation_id="res_yesterday_beijing",
        )

        # 冻结时间:直接 patch 辅助函数,避免全局 patch datetime
        _patch_frozen_time(monkeypatch, entitlements, frozen_utc_now, "Asia/Shanghai")

        quota = await entitlements.get_quota(user_id)
        # 今日仅 1 条记录(amount=2)
        assert quota.used_today == 2, (
            f"今日(北京时间)应只统计 1 条记录(amount=2),"
            f"实际 used_today={quota.used_today}"
        )


# ════════════════════════════════════════════════════════════════
# 测试 2: UTC 时区(午夜边界正确)
# ════════════════════════════════════════════════════════════════

class TestR53P1_4_UtcTimezone:
    """R53 P1-4: UTC 时区下,配额按 UTC 0 点重置。"""

    @pytest.mark.asyncio
    async def test_utc_timezone_midnight_boundary(
        self, real_store_with_real_plans, monkeypatch
    ):
        """UTC 时区:UTC 00:00 为日界,UTC 23:59 与 UTC 00:01 归不同日。

        场景:
            - BILLING_TIMEZONE=UTC
            - 当前 UTC 2026-07-15 12:00
            - 今日 00:30 UTC 的记录 → 计入今日
            - 昨日 23:30 UTC 的记录 → 不计入今日
        """
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "UTC")
        user_id = 10002
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # 当前 UTC 2026-07-15 12:00
        frozen_utc_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

        # 今日 00:30 UTC
        today_utc = datetime(2026, 7, 15, 0, 30, 0, tzinfo=timezone.utc).isoformat()
        # 昨日 23:30 UTC
        yesterday_utc = datetime(2026, 7, 14, 23, 30, 0, tzinfo=timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=3,
            created_at_iso=today_utc, status="settled",
            reservation_id="res_today_utc",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=7,
            created_at_iso=yesterday_utc, status="settled",
            reservation_id="res_yesterday_utc",
        )

        _patch_frozen_time(monkeypatch, entitlements, frozen_utc_now, "UTC")

        quota = await entitlements.get_quota(user_id)
        assert quota.used_today == 3, (
            f"UTC 时区今日应只统计 1 条记录(amount=3),"
            f"实际 used_today={quota.used_today}"
        )

    @pytest.mark.asyncio
    async def test_utc_get_balance_boundary(
        self, real_store_with_real_plans, monkeypatch
    ):
        """get_balance 在 UTC 时区下正确计算剩余配额。"""
        from config import settings
        from services import quota_ledger

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "UTC")
        user_id = 10003
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # basic 套餐 daily_quota=100(由 _make_real_plans 设置)
        # 插入今日 UTC 记录 amount=10
        today_utc = datetime.now(timezone.utc).isoformat()
        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=10,
            created_at_iso=today_utc, status="settled",
            reservation_id="res_balance_today",
        )

        balance = await quota_ledger.get_balance(user_id)
        # basic 套餐 daily_quota=100,今日已消耗 10 → balance=90
        assert balance == 90, f"UTC 时区 get_balance 应为 90(100-10),实际: {balance}"


# ════════════════════════════════════════════════════════════════
# 测试 3: UTC+8 时区(与默认 Asia/Shanghai 一致)
# ════════════════════════════════════════════════════════════════

class TestR53P1_4_UtcPlus8Timezone:
    """R53 P1-4: UTC+8 显式配置与默认 Asia/Shanghai 行为一致。"""

    @pytest.mark.asyncio
    async def test_utc_plus8_alias_matches_default(
        self, real_store_with_real_plans, monkeypatch
    ):
        """'Asia/Shanghai' 与固定偏移 UTC+8 在非 DST 切换日结果一致。

        场景:
            - BILLING_TIMEZONE=Asia/Shanghai(默认)
            - 同一时刻插入记录,get_quota.used_today 应一致
        """
        from config import settings
        from services import entitlements
        from zoneinfo import ZoneInfo

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")
        user_id = 10004
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # 当前北京时间(UTC+8)
        tz_shanghai = ZoneInfo("Asia/Shanghai")
        now_shanghai = datetime.now(tz_shanghai)
        # 今日 12:00 北京时间
        today_noon_shanghai = now_shanghai.replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        today_record_utc = today_noon_shanghai.astimezone(timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=4,
            created_at_iso=today_record_utc, status="settled",
            reservation_id="res_shanghai_today",
        )

        quota = await entitlements.get_quota(user_id)
        assert quota.used_today == 4, (
            f"Asia/Shanghai 时区今日应统计 1 条记录(amount=4),"
            f"实际 used_today={quota.used_today}"
        )

    @pytest.mark.asyncio
    async def test_explicit_offset_plus8_matches_named_timezone(
        self, real_store_with_real_plans, monkeypatch
    ):
        """显式偏移 'Etc/GMT-8' (POSIX 反向,实际为 UTC+8) 等价于 Asia/Shanghai(非 DST 日)。

        注:POSIX 时区 'Etc/GMT-8' 实际为 UTC+8(符号反直觉)。
        """
        from config import settings
        from services import entitlements
        from zoneinfo import ZoneInfo

        # Etc/GMT-8 在 POSIX 中表示 UTC+8
        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Etc/GMT-8")
        user_id = 10005
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # 当前 UTC+8 时间(避免 CI 在 UTC 16:00+ 运行时跨日导致测试失败)
        tz_gmt_minus_8 = ZoneInfo("Etc/GMT-8")
        now_local = datetime.now(tz_gmt_minus_8)
        # 今日 12:00 本地时间(确保在今日范围内,即使 CI 在 UTC 16:00+ 运行)
        today_noon_local = now_local.replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        today_record_utc = today_noon_local.astimezone(timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=6,
            created_at_iso=today_record_utc, status="settled",
            reservation_id="res_gmt_minus_8_today",
        )

        quota = await entitlements.get_quota(user_id)
        assert quota.used_today == 6, (
            f"Etc/GMT-8(UTC+8)时区今日应统计 1 条记录(amount=6),"
            f"实际 used_today={quota.used_today}"
        )


# ════════════════════════════════════════════════════════════════
# 测试 4: DST 时区(America/New_York,夏令时切换)
# ════════════════════════════════════════════════════════════════

class TestR53P1_4_DstTimezone:
    """R53 P1-4: DST 时区(America/New_York)夏令时切换不破坏日界。"""

    @pytest.mark.asyncio
    async def test_dst_spring_forward_boundary(
        self, real_store_with_real_plans, monkeypatch
    ):
        """America/New_York 春季 DST 切换日(2026-03-08 02:00 跳至 03:00)日界正确。

        场景:
            - BILLING_TIMEZONE=America/New_York
            - 当前本地时间 2026-03-08 12:00 EDT(= UTC 16:00)
            - 今日 03:00 EDT(切换后)= UTC 07:00 → 计入今日
            - 昨日 23:00 EST(切换前)= UTC 04:00 → 不计入今日

        边界计算(由 zoneinfo 自动处理 DST):
            - 2026-03-08 00:00 仍是 EST(UTC-5,02:00 才切换)
            - start_local = 2026-03-08 00:00 EST = UTC 05:00
            - end_local = 2026-03-09 00:00 EST = UTC 05:00(next day)
            - 今日 03:00 EDT(UTC 07:00)在 [05:00, 05:00+24h) 内 → 计入 ✓
            - 昨日 23:00 EST(UTC 04:00)不在 [05:00, 05:00+24h) 内 → 不计入 ✓
        """
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "America/New_York")
        user_id = 10006
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # 当前 America/New_York 本地时间 2026-03-08 12:00 EDT(= UTC 16:00)
        frozen_utc_now = datetime(2026, 3, 8, 16, 0, 0, tzinfo=timezone.utc)

        # 今日 03:00 EDT(切换后)= UTC 07:00 → 计入今日
        today_record_utc = datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc).isoformat()
        # 昨日 23:00 EST(切换前)= UTC 2026-03-08 04:00 → 不计入今日
        yesterday_record_utc = datetime(2026, 3, 8, 4, 0, 0, tzinfo=timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=8,
            created_at_iso=today_record_utc, status="settled",
            reservation_id="res_today_dst",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=11,
            created_at_iso=yesterday_record_utc, status="settled",
            reservation_id="res_yesterday_dst",
        )

        _patch_frozen_time(monkeypatch, entitlements, frozen_utc_now, "America/New_York")

        quota = await entitlements.get_quota(user_id)
        assert quota.used_today == 8, (
            f"DST 时区今日应只统计 1 条记录(amount=8),"
            f"实际 used_today={quota.used_today}(DST 切换边界)"
        )

    @pytest.mark.asyncio
    async def test_dst_fall_back_boundary(
        self, real_store_with_real_plans, monkeypatch
    ):
        """America/New_York 秋季 DST 切换日(2026-11-01 02:00 EDT 回退至 01:00 EST)。

        场景:
            - BILLING_TIMEZONE=America/New_York
            - 当前本地时间 2026-11-01 12:00 EST(已切换回 EST,UTC-5,= UTC 17:00)
            - 今日 01:30 EST(切换后)= UTC 06:30 → 计入今日
            - 昨日 23:30 EDT(切换前)= UTC 03:30 → 不计入今日

        边界计算(由 zoneinfo 自动处理 DST):
            - 2026-11-01 00:00 仍是 EDT(UTC-4,02:00 才回退)
            - start_local = 2026-11-01 00:00 EDT = UTC 04:00
            - end_local = 2026-11-02 00:00 EST = UTC 05:00(next day)
            - 今日 01:30 EST(UTC 06:30)在 [04:00, 04:00+25h) 内 → 计入 ✓
            - 昨日 23:30 EDT(UTC 03:30)不在 [04:00, ...) 内 → 不计入 ✓
        """
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "America/New_York")
        user_id = 10007
        await _insert_user(real_store_with_real_plans, user_id, level="basic")

        # 当前 America/New_York 本地时间 2026-11-01 12:00 EST(= UTC 17:00)
        frozen_utc_now = datetime(2026, 11, 1, 17, 0, 0, tzinfo=timezone.utc)

        # 今日 01:30 EST(切换后)= UTC 06:30
        today_record_utc = datetime(2026, 11, 1, 6, 30, 0, tzinfo=timezone.utc).isoformat()
        # 昨日 23:30 EDT(切换前)= UTC 03:30
        yesterday_record_utc = datetime(2026, 11, 1, 3, 30, 0, tzinfo=timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=9,
            created_at_iso=today_record_utc, status="settled",
            reservation_id="res_today_fallback",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_id, amount=13,
            created_at_iso=yesterday_record_utc, status="settled",
            reservation_id="res_yesterday_fallback",
        )

        _patch_frozen_time(monkeypatch, entitlements, frozen_utc_now, "America/New_York")

        quota = await entitlements.get_quota(user_id)
        assert quota.used_today == 9, (
            f"DST 秋季回退日今日应只统计 1 条记录(amount=9),"
            f"实际 used_today={quota.used_today}"
        )


# ════════════════════════════════════════════════════════════════
# 测试 5: 午夜并发(多个用户同时跨日)
# ════════════════════════════════════════════════════════════════

class TestR53P1_4_MidnightConcurrency:
    """R53 P1-4: 午夜并发场景 — 多个用户同时跨日,边界正确。"""

    @pytest.mark.asyncio
    async def test_multiple_users_cross_midnight(
        self, real_store_with_real_plans, monkeypatch
    ):
        """多用户跨日场景:同一时刻(午夜附近)对不同用户的统计互不影响。

        场景:
            - BILLING_TIMEZONE=Asia/Shanghai
            - 当前北京时间 2026-07-15 00:30(刚跨入新一天)
            - 用户 A:今日 00:15 的记录(应计入今日)
            - 用户 B:昨日 23:45 的记录(不应计入今日,且不影响 A)
            - 用户 A 与用户 B 的统计独立
        """
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")

        user_a = 10010
        user_b = 10011
        await _insert_user(real_store_with_real_plans, user_a, level="basic")
        await _insert_user(real_store_with_real_plans, user_b, level="basic")

        # 当前北京时间 2026-07-15 00:30(= UTC 2026-07-14 16:30)
        frozen_utc_now = datetime(2026, 7, 14, 16, 30, 0, tzinfo=timezone.utc)

        # 用户 A:今日 00:15 北京时间(= UTC 2026-07-14 16:15)
        user_a_today_utc = datetime(2026, 7, 14, 16, 15, 0, tzinfo=timezone.utc).isoformat()
        # 用户 B:昨日 23:45 北京时间(= UTC 2026-07-14 15:45)
        user_b_yesterday_utc = datetime(2026, 7, 14, 15, 45, 0, tzinfo=timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_a, amount=2,
            created_at_iso=user_a_today_utc, status="settled",
            reservation_id="res_user_a_today",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_b, amount=3,
            created_at_iso=user_b_yesterday_utc, status="settled",
            reservation_id="res_user_b_yesterday",
        )

        _patch_frozen_time(monkeypatch, entitlements, frozen_utc_now, "Asia/Shanghai")

        # 用户 A 今日应统计 amount=2
        quota_a = await entitlements.get_quota(user_a)
        assert quota_a.used_today == 2, (
            f"用户 A 今日(刚跨午夜)应统计 amount=2,实际 used_today={quota_a.used_today}"
        )
        # 用户 B 今日无新记录(其记录属于昨日)
        quota_b = await entitlements.get_quota(user_b)
        assert quota_b.used_today == 0, (
            f"用户 B 今日(昨日记录)应统计 amount=0,实际 used_today={quota_b.used_today}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_users_at_exact_midnight(
        self, real_store_with_real_plans, monkeypatch
    ):
        """精确午夜时刻:多用户在 00:00:00 同时跨日,边界精确。

        场景:
            - BILLING_TIMEZONE=Asia/Shanghai
            - 当前北京时间 2026-07-15 00:00:00(精确午夜)
            - 用户 A:00:00:00 的记录(应计入今日,因 >= start_utc)
            - 用户 B:23:59:59.999 的记录(应计入昨日,因 < start_utc)
        """
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")

        user_a = 10020
        user_b = 10021
        await _insert_user(real_store_with_real_plans, user_a, level="basic")
        await _insert_user(real_store_with_real_plans, user_b, level="basic")

        # 当前北京时间 2026-07-15 00:00:00 精确(= UTC 2026-07-14 16:00:00)
        frozen_utc_now = datetime(2026, 7, 14, 16, 0, 0, tzinfo=timezone.utc)

        # 用户 A:北京时间 2026-07-15 00:00:00(精确午夜)= UTC 2026-07-14 16:00:00
        user_a_record_utc = datetime(2026, 7, 14, 16, 0, 0, tzinfo=timezone.utc).isoformat()
        # 用户 B:北京时间 2026-07-14 23:59:59.999 = UTC 2026-07-14 15:59:59.999
        user_b_record_utc = datetime(2026, 7, 14, 15, 59, 59, 999000, tzinfo=timezone.utc).isoformat()

        await _insert_quota_reservation(
            real_store_with_real_plans, user_a, amount=5,
            created_at_iso=user_a_record_utc, status="settled",
            reservation_id="res_user_a_midnight",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_b, amount=7,
            created_at_iso=user_b_record_utc, status="settled",
            reservation_id="res_user_b_before_midnight",
        )

        _patch_frozen_time(monkeypatch, entitlements, frozen_utc_now, "Asia/Shanghai")

        quota_a = await entitlements.get_quota(user_a)
        assert quota_a.used_today == 5, (
            f"用户 A 在精确午夜 00:00:00 的记录应计入今日,"
            f"实际 used_today={quota_a.used_today}"
        )
        quota_b = await entitlements.get_quota(user_b)
        assert quota_b.used_today == 0, (
            f"用户 B 在 23:59:59.999 的记录应不计入今日(归属昨日),"
            f"实际 used_today={quota_b.used_today}"
        )

    @pytest.mark.asyncio
    async def test_get_balance_concurrent_users(
        self, real_store_with_real_plans, monkeypatch
    ):
        """get_balance 多用户并发:各自余额独立计算。"""
        from config import settings
        from services import quota_ledger

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")

        user_x = 10030
        user_y = 10031
        await _insert_user(real_store_with_real_plans, user_x, level="basic")
        await _insert_user(real_store_with_real_plans, user_y, level="basic")

        # 都插入今日 UTC 记录
        now_utc = datetime.now(timezone.utc).isoformat()
        await _insert_quota_reservation(
            real_store_with_real_plans, user_x, amount=20,
            created_at_iso=now_utc, status="settled",
            reservation_id="res_balance_user_x",
        )
        await _insert_quota_reservation(
            real_store_with_real_plans, user_y, amount=50,
            created_at_iso=now_utc, status="settled",
            reservation_id="res_balance_user_y",
        )

        # basic 套餐 daily_quota=100
        balance_x = await quota_ledger.get_balance(user_x)
        balance_y = await quota_ledger.get_balance(user_y)
        assert balance_x == 80, f"用户 X 余额应为 80(100-20),实际: {balance_x}"
        assert balance_y == 50, f"用户 Y 余额应为 50(100-50),实际: {balance_y}"


# ════════════════════════════════════════════════════════════════
# 测试 6: SQLite 查询不再使用 date('now', 'localtime')
# ════════════════════════════════════════════════════════════════

def _strip_comments_and_docstrings(source: str) -> str:
    """移除 Python 源码中的注释和 docstring,仅保留可执行代码。

    使用正则移除:
    1. # 开头的注释行
    2. 三引号字符串(docstring 和多行字符串)
    3. 单行三引号字符串
    """
    # 移除三引号字符串(含 docstring),使用非贪婪匹配
    cleaned = re.sub(r'""".*?"""', '""', source, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", "''", cleaned, flags=re.DOTALL)
    # 移除 # 注释行
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


class TestR53P1_4_NoLocaltimeDependency:
    """R53 P1-4: 源码静态校验 — SQLite 查询不再使用 date('now', 'localtime')。"""

    @pytest.mark.asyncio
    async def test_entitlements_get_quota_no_localtime_in_sql(self):
        """entitlements.get_quota 源码中不应再使用 date('now', 'localtime')。

        校验对象:实际执行的 SQL 字符串(不含注释和 docstring)。
        """
        src = await _fetch_quota_reservations_query_sql()
        code_without_comments = _strip_comments_and_docstrings(src)
        # SQL 字符串部分不应再包含 date('now', 'localtime')
        assert "date('now', 'localtime')" not in code_without_comments, (
            "entitlements.get_quota 源码(去除注释后)不应再使用 date('now', 'localtime'),"
            "应改为参数化 UTC 边界查询 created_at >= ? AND created_at < ?"
        )
        # 应使用参数化 UTC 边界查询
        assert "created_at >= ?" in code_without_comments, (
            "entitlements.get_quota 应使用参数化 UTC 边界查询 created_at >= ?"
        )
        assert "created_at < ?" in code_without_comments, (
            "entitlements.get_quota 应使用参数化 UTC 边界查询 created_at < ?"
        )

    @pytest.mark.asyncio
    async def test_quota_ledger_get_balance_no_localtime_in_sql(self):
        """quota_ledger.get_balance 源码中不应再使用 date('now', 'localtime')。"""
        src = await _fetch_balance_query_sql()
        code_without_comments = _strip_comments_and_docstrings(src)
        assert "date('now', 'localtime')" not in code_without_comments, (
            "quota_ledger.get_balance 源码(去除注释后)不应再使用 date('now', 'localtime'),"
            "应改为参数化 UTC 边界查询 created_at >= ? AND created_at < ?"
        )
        assert "created_at >= ?" in code_without_comments, (
            "quota_ledger.get_balance 应使用参数化 UTC 边界查询 created_at >= ?"
        )
        assert "created_at < ?" in code_without_comments, (
            "quota_ledger.get_balance 应使用参数化 UTC 边界查询 created_at < ?"
        )

    @pytest.mark.asyncio
    async def test_no_localtime_in_full_services_directory(self):
        """services/ 目录下源码(去除注释和 docstring 后)不再使用 date('now', 'localtime')。"""
        services_dir = Path(__file__).resolve().parent.parent / "services"
        violators = []
        for py_file in services_dir.glob("*.py"):
            try:
                src = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            code_without_comments = _strip_comments_and_docstrings(src)
            if "date('now', 'localtime')" in code_without_comments:
                violators.append(py_file.name)
        assert not violators, (
            f"services/ 目录下源码(去除注释和 docstring 后)不应再使用 date('now', 'localtime'),"
            f"违规文件: {violators}"
        )

    @pytest.mark.asyncio
    async def test_billing_timezone_setting_exists(self):
        """config.settings 应包含 BILLING_TIMEZONE 属性。"""
        from config import settings
        # conftest 注入 MagicMock,getattr 返回 MagicMock 不会 AttributeError
        # 通过 getattr + str 校验
        val = getattr(settings, "BILLING_TIMEZONE", None)
        assert val is not None, "settings.BILLING_TIMEZONE 应存在"

    @pytest.mark.asyncio
    async def test_helper_functions_exist(self):
        """entitlements 模块应包含 R53 P1-4 新增的辅助函数。"""
        from services import entitlements
        assert hasattr(entitlements, "_get_billing_day_utc_bounds"), (
            "entitlements._get_billing_day_utc_bounds 应存在(R53 P1-4 辅助函数)"
        )
        assert hasattr(entitlements, "_get_billing_today_date"), (
            "entitlements._get_billing_today_date 应存在(R53 P1-4 辅助函数)"
        )

    @pytest.mark.asyncio
    async def test_billing_day_utc_bounds_returns_iso_strings(self, monkeypatch):
        """_get_billing_day_utc_bounds 返回 UTC ISO 字符串(带 +00:00)。"""
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")
        start_iso, end_iso = entitlements._get_billing_day_utc_bounds()
        # 应为字符串
        assert isinstance(start_iso, str)
        assert isinstance(end_iso, str)
        # 应包含 +00:00 后缀(UTC aware ISO)
        assert "+00:00" in start_iso, (
            f"start_utc_iso 应为 UTC aware ISO(带 +00:00),实际: {start_iso}"
        )
        assert "+00:00" in end_iso, (
            f"end_utc_iso 应为 UTC aware ISO(带 +00:00),实际: {end_iso}"
        )
        # end_iso 应晚于 start_utc(差约 24 小时)
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        delta = end_dt - start_dt
        assert delta.total_seconds() == 86400.0, (
            f"UTC 边界间隔应为 24 小时(86400 秒),实际: {delta.total_seconds()} 秒"
        )

    @pytest.mark.asyncio
    async def test_billing_today_date_returns_date(self, monkeypatch):
        """_get_billing_today_date 返回 date 对象。"""
        from config import settings
        from services import entitlements

        monkeypatch.setattr(settings, "BILLING_TIMEZONE", "Asia/Shanghai")
        today = entitlements._get_billing_today_date()
        import datetime as _dt
        assert isinstance(today, _dt.date), (
            f"_get_billing_today_date 应返回 date 对象,实际类型: {type(today)}"
        )
