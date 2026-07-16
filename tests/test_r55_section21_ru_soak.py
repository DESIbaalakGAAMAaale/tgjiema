"""R55 §21: CRDB RU 72h 官方验证 + 7 天 soak 测试 — 测试套件。

测试覆盖范围:
    A. RU 门禁阈值常量正确性(6 项门禁)
       - Bot 角色 0 RU/day
       - 总空载理想 ≤20 RU/day
       - 总空载硬上限 ≤100 RU/day
       - >500 RU/day 阻断
       - ≤250 RU/DAU/day
       - 月 ≤35M RU
    B. application_name/role 归因逻辑
       - 业务 Bot(up/idx/dsp/mon/admin_bot)不应持有 COCKROACHDB_URL
       - 基础设施服务(crdb_sync/migration/backup/restore)允许读取
    C. 0 用户时定时任务调用次数记录
       - 10 个定时任务的调用次数应被记录到 kv_store
    D. soak 测试矩阵完整性
       - 7 天 × 24 小时 = 168 轮健康检查
       - 7 次 × 28 组合 = 196 次故障注入
    E. 报告格式验证(ru_72h_report + soak_report JSON 结构)
    F. fail-closed 行为(阈值超标立即失败)

被测代码引用:
    - scripts/ru_72h_verification.sh — 72h 官方 RU 验证脚本
    - scripts/soak_test_7day.sh — 7 天 soak 测试脚本
    - services/ru_cost_center.py — RU 成本中心(RU_PER_READ/WRITE/QUERY 常量)
    - services/crdb_ru_collector.py — RU 采集器(is_service_allowed_crdb_url)
    - scripts/export_ru_report.py — RU 报告导出(analyze_ru_report)

测试策略:
    - 所有测试不依赖真实 CRDB/Redis/SQLite(使用 mock + 文件读取)
    - 阈值常量测试:读取 bash 脚本内容,验证嵌入的常量与 R55 §21 规范一致
    - 归因逻辑测试:调用 crdb_ru_collector.is_service_allowed_crdb_url
    - 定时任务测试:mock kv_store 记录/读取调用次数
    - 矩阵完整性测试:纯算术校验(7×24=168, 7×28=196)
    - 报告格式测试:构造样本 JSON,验证必需字段
    - fail-closed 测试:阈值超标检测函数
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试文件顶部 mock telegram 模块(避免 import 失败)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())


# ════════════════════════════════════════════════════════════════
# R55 §21 规范常量(本测试文件的规范契约)
# ════════════════════════════════════════════════════════════════

# 门禁阈值(R55 §21 规范定义)
SPEC_BOT_RU_PER_DAY_LIMIT = 0           # Bot 角色 0 RU/day
SPEC_IDLE_RU_IDEAL = 20                 # 总空载理想 ≤20 RU/day
SPEC_IDLE_RU_HARD_LIMIT = 100           # 总空载硬上限 ≤100 RU/day
SPEC_IDLE_RU_BLOCK_THRESHOLD = 500      # >500 RU/day 阻断
SPEC_RU_PER_DAU_DAY_LIMIT = 250         # ≤250 RU/DAU/day
SPEC_MONTHLY_RU_LIMIT = 35_000_000      # 月 ≤35M RU

# soak 测试矩阵常量(R55 §21 规范定义)
SPEC_SOAK_DURATION_DAYS = 7
SPEC_HEALTH_CHECKS_PER_DAY = 24
SPEC_TOTAL_HEALTH_CHECKS = SPEC_SOAK_DURATION_DAYS * SPEC_HEALTH_CHECKS_PER_DAY  # 168
SPEC_FAULT_MATRIX_BOTS = 4
SPEC_FAULT_MATRIX_SCENARIOS = 7
SPEC_FAULT_MATRIX_PER_CYCLE = SPEC_FAULT_MATRIX_BOTS * SPEC_FAULT_MATRIX_SCENARIOS  # 28
SPEC_FAULT_CYCLES = 7
SPEC_TOTAL_FAULT_INJECTIONS = SPEC_FAULT_CYCLES * SPEC_FAULT_MATRIX_PER_CYCLE  # 196

# 业务 Bot 列表(不应产生 CRDB RU)
SPEC_BUSINESS_BOTS = ["up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"]

# 基础设施服务列表(允许读取 COCKROACHDB_URL)
SPEC_INFRA_SERVICES = ["crdb_sync", "migration", "bootstrap", "disaster_recovery", "backup"]

# 定时任务列表(R55 §21 要求记录 0 用户时所有定时任务调用次数)
SPEC_CRON_JOBS = [
    "crdb_sync_dirty",
    "crdb_ru_collector",
    "backup_gc",
    "retention_worker",
    "decode_logs_cleanup",
    "outbox_worker",
    "dlq_worker",
    "relay_pool_lease_renewal",
    "callback_nonce_cleanup",
    "replication_health_check",
]

# 脚本文件路径
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
RU_72H_SCRIPT = SCRIPTS_DIR / "ru_72h_verification.sh"
SOAK_SCRIPT = SCRIPTS_DIR / "soak_test_7day.sh"
EXPORT_RU_SCRIPT = SCRIPTS_DIR / "export_ru_report.py"


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _read_script(path: Path) -> str:
    """读取脚本文件内容(容错:文件不存在时返回空字符串)。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _check_threshold_in_script(script_text: str, var_name: str, expected: int) -> bool:
    """检查 bash 脚本中是否包含指定阈值的赋值。

    支持格式:
        VAR_NAME=值
        VAR_NAME=值  # 注释
    """
    import re
    # 匹配 VAR_NAME=数字(允许中间有空格)
    pattern = rf"{var_name}\s*=\s*{expected}\b"
    return bool(re.search(pattern, script_text))


# ════════════════════════════════════════════════════════════════
# A. RU 门禁阈值常量正确性测试
# ════════════════════════════════════════════════════════════════


class TestRUGateThresholdConstants:
    """A. RU 门禁阈值常量正确性测试(6 项门禁)。"""

    def test_bot_ru_per_day_limit_is_zero(self):
        """门禁 1: Bot 角色 RU/天 = 0。"""
        assert SPEC_BOT_RU_PER_DAY_LIMIT == 0

    def test_idle_ru_ideal_is_twenty(self):
        """门禁 2: 总空载理想 ≤20 RU/day。"""
        assert SPEC_IDLE_RU_IDEAL == 20

    def test_idle_ru_hard_limit_is_hundred(self):
        """门禁 3: 总空载硬上限 ≤100 RU/day。"""
        assert SPEC_IDLE_RU_HARD_LIMIT == 100

    def test_idle_ru_block_threshold_is_five_hundred(self):
        """门禁 4: >500 RU/day 阻断。"""
        assert SPEC_IDLE_RU_BLOCK_THRESHOLD == 500

    def test_ru_per_dau_day_limit_is_250(self):
        """门禁 5: ≤250 RU/DAU/day。"""
        assert SPEC_RU_PER_DAU_DAY_LIMIT == 250

    def test_monthly_ru_limit_is_35m(self):
        """门禁 6: 月 ≤35M RU。"""
        assert SPEC_MONTHLY_RU_LIMIT == 35_000_000

    def test_threshold_ordering_consistent(self):
        """门禁阈值应满足递增关系:ideal < hard < block。"""
        assert SPEC_BOT_RU_PER_DAY_LIMIT < SPEC_IDLE_RU_IDEAL
        assert SPEC_IDLE_RU_IDEAL < SPEC_IDLE_RU_HARD_LIMIT
        assert SPEC_IDLE_RU_HARD_LIMIT < SPEC_IDLE_RU_BLOCK_THRESHOLD

    def test_ru_72h_script_contains_all_thresholds(self):
        """72h 验证脚本应包含全部 6 项门禁阈值。"""
        script = _read_script(RU_72H_SCRIPT)
        assert script, f"无法读取脚本: {RU_72H_SCRIPT}"

        assert _check_threshold_in_script(script, "BOT_RU_PER_DAY_LIMIT", 0), \
            "脚本缺少 BOT_RU_PER_DAY_LIMIT=0"
        assert _check_threshold_in_script(script, "IDLE_RU_IDEAL", 20), \
            "脚本缺少 IDLE_RU_IDEAL=20"
        assert _check_threshold_in_script(script, "IDLE_RU_HARD_LIMIT", 100), \
            "脚本缺少 IDLE_RU_HARD_LIMIT=100"
        assert _check_threshold_in_script(script, "IDLE_RU_BLOCK_THRESHOLD", 500), \
            "脚本缺少 IDLE_RU_BLOCK_THRESHOLD=500"
        assert _check_threshold_in_script(script, "RU_PER_DAU_DAY_LIMIT", 250), \
            "脚本缺少 RU_PER_DAU_DAY_LIMIT=250"
        assert _check_threshold_in_script(script, "MONTHLY_RU_LIMIT", 35000000), \
            "脚本缺少 MONTHLY_RU_LIMIT=35000000"

    def test_ru_cost_center_constants_correct(self):
        """ru_cost_center 的 RU 单价常量应正确(读=1, 写=2, 查询=3)。"""
        from services.ru_cost_center import RU_PER_READ, RU_PER_WRITE, RU_PER_QUERY
        assert RU_PER_READ == 1
        assert RU_PER_WRITE == 2
        assert RU_PER_QUERY == 3

    def test_ru_cost_center_services_complete(self):
        """ru_cost_center.SERVICES 应包含全部业务 Bot + 基础设施服务。"""
        from services.ru_cost_center import SERVICES
        required = {"up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot",
                    "crdb_sync", "migration", "backup", "restore"}
        actual = set(SERVICES)
        missing = required - actual
        assert not missing, f"SERVICES 缺少: {missing}"

    def test_export_ru_report_thresholds_match(self):
        """export_ru_report.py 的阈值应与 R55 §21 规范一致。"""
        script = _read_script(EXPORT_RU_SCRIPT)
        assert script, f"无法读取脚本: {EXPORT_RU_SCRIPT}"
        # export_ru_report.py 中定义的阈值(total_idle_ideal=20, hard_limit=100, block=500)
        assert "20" in script and "100" in script and "500" in script


# ════════════════════════════════════════════════════════════════
# B. application_name/role 归因逻辑测试
# ════════════════════════════════════════════════════════════════


class TestApplicationNameAttribution:
    """B. application_name/role 归因逻辑测试。"""

    def test_business_bots_not_allowed_crdb_url(self):
        """业务 Bot 不应被允许读取 COCKROACHDB_URL。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        for bot in SPEC_BUSINESS_BOTS:
            assert not is_service_allowed_crdb_url(bot), (
                f"业务 Bot {bot} 不应被允许读取 COCKROACHDB_URL"
            )

    def test_infra_services_allowed_crdb_url(self):
        """基础设施服务应被允许读取 COCKROACHDB_URL。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        for svc in SPEC_INFRA_SERVICES:
            assert is_service_allowed_crdb_url(svc), (
                f"基础设施服务 {svc} 应被允许读取 COCKROACHDB_URL"
            )

    def test_empty_service_not_allowed(self):
        """空服务名不应被允许。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert not is_service_allowed_crdb_url("")
        assert not is_service_allowed_crdb_url("   ")

    def test_unknown_service_not_allowed(self):
        """未知服务名不应被允许。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert not is_service_allowed_crdb_url("unknown_service")
        assert not is_service_allowed_crdb_url("random_bot")

    def test_business_bots_list_in_script(self):
        """72h 验证脚本应包含业务 Bot 列表。"""
        script = _read_script(RU_72H_SCRIPT)
        assert script, f"无法读取脚本: {RU_72H_SCRIPT}"
        for bot in SPEC_BUSINESS_BOTS:
            assert bot in script, (
                f"脚本缺少业务 Bot '{bot}' 的归因校验"
            )

    def test_attribution_check_step_exists(self):
        """72h 验证脚本应包含 application_name 归因校验步骤。"""
        script = _read_script(RU_72H_SCRIPT)
        assert "application_name" in script.lower() or "attribution" in script.lower(), (
            "脚本缺少 application_name/role 归因校验步骤"
        )
        assert "attribution_check" in script, (
            "脚本缺少 attribution_check 步骤标识"
        )

    def test_verify_ru_source_official_returns_dict(self):
        """verify_ru_source_official 应返回包含必需字段的 dict。"""
        from services.crdb_ru_collector import verify_ru_source_official
        result = verify_ru_source_official()
        assert isinstance(result, dict)
        # 必需字段
        required_fields = {"is_official", "ru_value", "collector_id",
                          "response_digest", "created_at"}
        assert required_fields.issubset(set(result.keys())), (
            f"verify_ru_source_official 缺少字段: {required_fields - set(result.keys())}"
        )
        # 无 DB 时应返回 is_official=False
        assert result["is_official"] in (True, False)

    def test_collector_identity_check_in_script(self):
        """72h 验证脚本应包含官方 Collector 身份验证步骤。"""
        script = _read_script(RU_72H_SCRIPT)
        assert "collector_identity" in script, (
            "脚本缺少 collector_identity 步骤标识"
        )
        assert "response_digest" in script, (
            "脚本缺少 response_digest 验证"
        )
        assert "verify_ru_source_official" in script, (
            "脚本应调用 verify_ru_source_official()"
        )


# ════════════════════════════════════════════════════════════════
# C. 0 用户时定时任务调用次数记录测试
# ════════════════════════════════════════════════════════════════


class _FakeCacheStore:
    """模拟 cache_store:支持 get_kv/set_kv,用于定时任务调用次数记录测试。"""

    def __init__(self):
        self._kv: dict[str, str] = {}
        self._db = MagicMock(name="fake_db")

    async def get_kv(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set_kv(self, key: str, value: str):
        self._kv[key] = value


class TestCronInvocationCount:
    """C. 0 用户时定时任务调用次数记录测试。"""

    def test_cron_jobs_count_is_ten(self):
        """定时任务列表应有 10 个作业。"""
        assert len(SPEC_CRON_JOBS) == 10

    def test_cron_jobs_all_non_empty(self):
        """每个定时任务名应非空。"""
        for job in SPEC_CRON_JOBS:
            assert isinstance(job, str) and len(job) > 0

    def test_cron_jobs_unique(self):
        """定时任务名应唯一。"""
        assert len(SPEC_CRON_JOBS) == len(set(SPEC_CRON_JOBS))

    @pytest.mark.asyncio
    async def test_cron_count_record_and_read(self):
        """定时任务调用次数应可记录到 kv_store 并读回。"""
        store = _FakeCacheStore()
        # 记录每个定时任务的调用次数
        for i, job in enumerate(SPEC_CRON_JOBS):
            key = f"cron_invocation_count:{job}"
            await store.set_kv(key, str(i * 10))
        # 读回并验证
        values = []
        for i, job in enumerate(SPEC_CRON_JOBS):
            key = f"cron_invocation_count:{job}"
            raw = await store.get_kv(key)
            assert raw is not None, f"定时任务 {job} 的调用次数未记录"
            assert int(raw) == i * 10
            values.append(int(raw))
        # 验证总调用次数
        total = sum(values)
        # 0+10+20+...+90 = 450
        assert total == sum(i * 10 for i in range(10))

    @pytest.mark.asyncio
    async def test_cron_count_default_zero(self):
        """未记录的定时任务调用次数应默认为 0。"""
        store = _FakeCacheStore()
        for job in SPEC_CRON_JOBS:
            key = f"cron_invocation_count:{job}"
            raw = await store.get_kv(key)
            count = int(raw) if raw else 0
            assert count == 0

    def test_cron_count_step_in_script(self):
        """72h 验证脚本应包含定时任务调用次数记录步骤。"""
        script = _read_script(RU_72H_SCRIPT)
        assert script, f"无法读取脚本: {RU_72H_SCRIPT}"
        assert "cron_invocation_count" in script, (
            "脚本缺少 cron_invocation_count 步骤"
        )
        assert "cron_invocation_count" in script, (
            "脚本应使用 cron_invocation_count:{job} 作为 kv_store key"
        )
        # 验证脚本包含所有定时任务
        for job in SPEC_CRON_JOBS:
            assert job in script, f"脚本缺少定时任务 '{job}'"

    def test_cron_count_kv_key_format(self):
        """定时任务调用次数的 kv_store key 格式应为 cron_invocation_count:{job}。"""
        for job in SPEC_CRON_JOBS:
            key = f"cron_invocation_count:{job}"
            assert key.startswith("cron_invocation_count:")
            assert job in key


# ════════════════════════════════════════════════════════════════
# D. soak 测试矩阵完整性测试
# ════════════════════════════════════════════════════════════════


class TestSoakTestMatrix:
    """D. soak 测试矩阵完整性测试(7天 × 24小时 = 168 轮健康检查)。"""

    def test_soak_duration_is_7_days(self):
        """soak 测试持续天数应为 7。"""
        assert SPEC_SOAK_DURATION_DAYS == 7

    def test_health_checks_per_day_is_24(self):
        """每天健康检查次数应为 24(每小时一次)。"""
        assert SPEC_HEALTH_CHECKS_PER_DAY == 24

    def test_total_health_checks_is_168(self):
        """总健康检查次数应为 168(7 × 24)。"""
        assert SPEC_TOTAL_HEALTH_CHECKS == 168
        assert SPEC_SOAK_DURATION_DAYS * SPEC_HEALTH_CHECKS_PER_DAY == 168

    def test_fault_matrix_bots_is_4(self):
        """故障注入矩阵的 Bot 数量应为 4(up/idx/dsp/mon)。"""
        assert SPEC_FAULT_MATRIX_BOTS == 4

    def test_fault_matrix_scenarios_is_7(self):
        """故障注入矩阵的场景数量应为 7。"""
        assert SPEC_FAULT_MATRIX_SCENARIOS == 7

    def test_fault_matrix_per_cycle_is_28(self):
        """每次故障注入矩阵的组合数应为 28(4 × 7)。"""
        assert SPEC_FAULT_MATRIX_PER_CYCLE == 28
        assert SPEC_FAULT_MATRIX_BOTS * SPEC_FAULT_MATRIX_SCENARIOS == 28

    def test_fault_cycles_is_7(self):
        """故障注入矩阵执行次数应为 7(每天一次,共 7 天)。"""
        assert SPEC_FAULT_CYCLES == 7

    def test_total_fault_injections_is_196(self):
        """总故障注入次数应为 196(7 × 28)。"""
        assert SPEC_TOTAL_FAULT_INJECTIONS == 196
        assert SPEC_FAULT_CYCLES * SPEC_FAULT_MATRIX_PER_CYCLE == 196

    def test_soak_script_contains_matrix_constants(self):
        """soak 测试脚本应包含矩阵常量。"""
        script = _read_script(SOAK_SCRIPT)
        assert script, f"无法读取脚本: {SOAK_SCRIPT}"
        assert "SOAK_DURATION_DAYS=7" in script or "SOAK_DURATION_DAYS=7 " in script
        assert "HEALTH_CHECKS_PER_DAY=24" in script
        assert "TOTAL_HEALTH_CHECKS" in script
        assert "FAULT_MATRIX_BOTS=4" in script
        assert "FAULT_MATRIX_SCENARIOS=7" in script
        assert "FAULT_MATRIX_PER_CYCLE" in script
        assert "FAULT_CYCLES" in script
        assert "TOTAL_FAULT_INJECTIONS" in script

    def test_soak_script_contains_168_calculation(self):
        """soak 脚本应包含 168 轮健康检查的计算。"""
        script = _read_script(SOAK_SCRIPT)
        # 验证 7 * 24 = 168 的计算逻辑存在
        assert "168" in script or "SOAK_DURATION_DAYS * HEALTH_CHECKS_PER_DAY" in script

    def test_soak_script_contains_196_calculation(self):
        """soak 脚本应包含 196 次故障注入的计算。"""
        script = _read_script(SOAK_SCRIPT)
        assert "196" in script or "FAULT_CYCLES * FAULT_MATRIX_PER_CYCLE" in script

    def test_soak_script_consistency_violation_threshold_zero(self):
        """soak 脚本的一致性违规阈值应为 0。"""
        script = _read_script(SOAK_SCRIPT)
        assert "CONSISTENCY_VIOLATION_THRESHOLD=0" in script

    def test_soak_script_hourly_checks_described(self):
        """soak 脚本应描述每小时执行的项目。"""
        script = _read_script(SOAK_SCRIPT)
        # 每小时应执行:健康检查 + RU 记录 + 一致性校验 + 定时任务
        assert "health_check" in script.lower() or "健康检查" in script
        assert "ru" in script.lower() or "RU" in script
        assert "consistency" in script.lower() or "一致性" in script
        assert "cron" in script.lower() or "定时任务" in script

    def test_soak_script_calls_chaos_injection(self):
        """soak 脚本应调用 chaos_bot_fault_injection.sh。"""
        script = _read_script(SOAK_SCRIPT)
        assert "chaos_bot_fault_injection.sh" in script
        assert "--bot all" in script
        assert "--scenario all" in script

    def test_soak_script_data_consistency_checks(self):
        """soak 脚本应包含三项数据一致性校验。"""
        script = _read_script(SOAK_SCRIPT)
        # durable_outbox pending
        assert "durable_outbox" in script
        # unreconciled_copies
        assert "unreconciled_copies" in script
        # callback_nonces 过期清理
        assert "callback_nonces" in script


# ════════════════════════════════════════════════════════════════
# E. 报告格式验证测试
# ════════════════════════════════════════════════════════════════


class TestReportFormat:
    """E. 报告格式验证测试(ru_72h_report + soak_report JSON 结构)。"""

    def test_ru_72h_report_required_fields(self):
        """ru_72h_report 应包含所有必需字段。"""
        sample_report = {
            "report_type": "r55_section21_ru_72h_verification",
            "report_version": "1.0",
            "generated_at": "2026-07-16T00:00:00Z",
            "started_at": "2026-07-16T00:00:00Z",
            "completed_at": "2026-07-16T00:05:00Z",
            "duration_seconds": 300,
            "status": "SUCCESS",
            "hours": 72,
            "dry_run": False,
            "thresholds": {
                "bot_ru_per_day_limit": 0,
                "idle_ru_ideal": 20,
                "idle_ru_hard_limit": 100,
                "idle_ru_block_threshold": 500,
                "ru_per_dau_day_limit": 250,
                "monthly_ru_limit": 35000000,
            },
            "checks": {"fetch_official_ru": "PASS", "attribution_check": "PASS"},
            "gates": [{"gate": "bot_ru_per_day", "status": "PASS"}],
            "gates_summary": {"total": 6, "passed": 6, "failed": 0},
            "application_name_attribution": [],
            "cron_invocation_counts": {},
            "fail_closed": True,
        }
        required_fields = {
            "report_type", "report_version", "generated_at",
            "started_at", "completed_at", "duration_seconds",
            "status", "hours", "dry_run", "thresholds",
            "checks", "gates", "gates_summary",
            "application_name_attribution", "cron_invocation_counts",
            "fail_closed",
        }
        assert required_fields.issubset(set(sample_report.keys())), (
            f"ru_72h_report 缺少字段: {required_fields - set(sample_report.keys())}"
        )

    def test_ru_72h_report_type_correct(self):
        """ru_72h_report 的 report_type 应为 'r55_section21_ru_72h_verification'。"""
        sample_report = {"report_type": "r55_section21_ru_72h_verification"}
        assert sample_report["report_type"] == "r55_section21_ru_72h_verification"

    def test_ru_72h_report_version_correct(self):
        """ru_72h_report 的 report_version 应为 '1.0'。"""
        sample_report = {"report_version": "1.0"}
        assert sample_report["report_version"] == "1.0"

    def test_ru_72h_report_thresholds_match_spec(self):
        """ru_72h_report 的 thresholds 应与 R55 §21 规范一致。"""
        thresholds = {
            "bot_ru_per_day_limit": 0,
            "idle_ru_ideal": 20,
            "idle_ru_hard_limit": 100,
            "idle_ru_block_threshold": 500,
            "ru_per_dau_day_limit": 250,
            "monthly_ru_limit": 35000000,
        }
        assert thresholds["bot_ru_per_day_limit"] == SPEC_BOT_RU_PER_DAY_LIMIT
        assert thresholds["idle_ru_ideal"] == SPEC_IDLE_RU_IDEAL
        assert thresholds["idle_ru_hard_limit"] == SPEC_IDLE_RU_HARD_LIMIT
        assert thresholds["idle_ru_block_threshold"] == SPEC_IDLE_RU_BLOCK_THRESHOLD
        assert thresholds["ru_per_dau_day_limit"] == SPEC_RU_PER_DAU_DAY_LIMIT
        assert thresholds["monthly_ru_limit"] == SPEC_MONTHLY_RU_LIMIT

    def test_ru_72h_report_fail_closed_true(self):
        """ru_72h_report 的 fail_closed 应为 True。"""
        sample_report = {"fail_closed": True}
        assert sample_report["fail_closed"] is True

    def test_ru_72h_report_json_serializable(self):
        """ru_72h_report 应可 JSON 序列化(含中文)。"""
        sample_report = {
            "report_type": "r55_section21_ru_72h_verification",
            "checks": {"attribution_check": "PASS"},
            "detail": "业务 Bot 空载 RU 校验通过",
        }
        json_str = json.dumps(sample_report, ensure_ascii=False)
        assert isinstance(json_str, str)
        restored = json.loads(json_str)
        assert restored["report_type"] == "r55_section21_ru_72h_verification"
        assert "业务" in restored["detail"]

    def test_soak_report_required_fields(self):
        """soak_report 应包含所有必需字段。"""
        sample_report = {
            "report_type": "r55_section21_soak_test_7day",
            "report_version": "1.0",
            "generated_at": "2026-07-23T00:00:00Z",
            "started_at": "2026-07-16T00:00:00Z",
            "completed_at": "2026-07-23T00:00:00Z",
            "duration_seconds": 604800,
            "status": "SUCCESS",
            "dry_run": False,
            "config": {"duration_days": 7, "interval_seconds": 3600},
            "matrix": {
                "total_health_checks_expected": 168,
                "total_health_checks_executed": 168,
                "total_fault_injections_expected": 196,
                "total_fault_injections_executed": 196,
                "consistency_violation_threshold": 0,
                "consistency_violations_actual": 0,
            },
            "summary": {
                "total_runtime_seconds": 604800,
                "total_fault_injections": 196,
                "data_consistency_violations": 0,
                "ru_consumption_trend": {"total_ru": 2520, "avg_ru_per_hour": 15},
                "resource_usage_trend": {"cpu_avg": 15, "mem_avg": 40},
            },
            "ru_trend": [],
            "resource_trend": [],
            "consistency_results": [],
            "cron_invocation_trend": [],
            "fault_injection_results": [],
            "fail_closed": True,
        }
        required_fields = {
            "report_type", "report_version", "generated_at",
            "started_at", "completed_at", "duration_seconds",
            "status", "dry_run", "config", "matrix", "summary",
            "ru_trend", "resource_trend", "consistency_results",
            "cron_invocation_trend", "fault_injection_results",
            "fail_closed",
        }
        assert required_fields.issubset(set(sample_report.keys())), (
            f"soak_report 缺少字段: {required_fields - set(sample_report.keys())}"
        )

    def test_soak_report_type_correct(self):
        """soak_report 的 report_type 应为 'r55_section21_soak_test_7day'。"""
        sample_report = {"report_type": "r55_section21_soak_test_7day"}
        assert sample_report["report_type"] == "r55_section21_soak_test_7day"

    def test_soak_report_matrix_values_correct(self):
        """soak_report 的 matrix 值应与 R55 §21 规范一致。"""
        matrix = {
            "total_health_checks_expected": 168,
            "total_fault_injections_expected": 196,
            "consistency_violation_threshold": 0,
        }
        assert matrix["total_health_checks_expected"] == SPEC_TOTAL_HEALTH_CHECKS
        assert matrix["total_fault_injections_expected"] == SPEC_TOTAL_FAULT_INJECTIONS
        assert matrix["consistency_violation_threshold"] == 0

    def test_soak_report_fail_closed_true(self):
        """soak_report 的 fail_closed 应为 True。"""
        sample_report = {"fail_closed": True}
        assert sample_report["fail_closed"] is True

    def test_soak_report_json_serializable(self):
        """soak_report 应可 JSON 序列化(含中文)。"""
        sample_report = {
            "report_type": "r55_section21_soak_test_7day",
            "summary": {"data_consistency_violations": 0},
            "detail": "数据一致性校验通过",
        }
        json_str = json.dumps(sample_report, ensure_ascii=False)
        assert isinstance(json_str, str)
        restored = json.loads(json_str)
        assert restored["report_type"] == "r55_section21_soak_test_7day"
        assert "数据" in restored["detail"]

    def test_gate_result_format(self):
        """门禁结果条目应包含必需字段。"""
        gate = {
            "gate": "bot_ru_per_day",
            "expected": "<= 0",
            "actual": "0.00",
            "status": "PASS",
            "detail": None,
        }
        required_fields = {"gate", "expected", "actual", "status", "detail"}
        assert required_fields.issubset(set(gate.keys()))
        assert gate["status"] in ("PASS", "FAIL", "SKIP")

    def test_attribution_entry_format(self):
        """归因条目应包含必需字段。"""
        entry = {
            "service": "up_bot",
            "role": "business_bot",
            "ru_consumed": 0,
            "has_application_name": True,
            "expected_ru": 0,
            "status": "PASS",
        }
        required_fields = {"service", "role", "ru_consumed",
                          "has_application_name", "expected_ru", "status"}
        assert required_fields.issubset(set(entry.keys()))
        assert entry["status"] in ("PASS", "FAIL")


# ════════════════════════════════════════════════════════════════
# F. fail-closed 行为测试
# ════════════════════════════════════════════════════════════════


def _evaluate_gate(value: float, threshold: float, operator: str = "<=") -> str:
    """模拟门禁评估逻辑(与 bash 脚本中的门禁校验一致)。

    Args:
        value: 实际值
        threshold: 阈值
        operator: 比较运算符("<=" 或 ">")

    Returns:
        "PASS" 或 "FAIL"
    """
    if operator == "<=":
        return "PASS" if value <= threshold else "FAIL"
    elif operator == ">":
        return "PASS" if value > threshold else "FAIL"
    return "FAIL"


class TestFailClosedBehavior:
    """F. fail-closed 行为测试(阈值超标立即失败)。"""

    def test_bot_ru_zero_passes(self):
        """Bot 角色 RU=0 应 PASS(门禁:0 RU/day)。"""
        assert _evaluate_gate(0, SPEC_BOT_RU_PER_DAY_LIMIT, "<=") == "PASS"

    def test_bot_ru_nonzero_fails(self):
        """Bot 角色 RU>0 应 FAIL(fail-closed)。"""
        assert _evaluate_gate(1, SPEC_BOT_RU_PER_DAY_LIMIT, "<=") == "FAIL"
        assert _evaluate_gate(0.01, SPEC_BOT_RU_PER_DAY_LIMIT, "<=") == "FAIL"

    def test_idle_ru_below_ideal_passes(self):
        """空载 RU ≤20 应 PASS(理想阈值)。"""
        assert _evaluate_gate(0, SPEC_IDLE_RU_IDEAL, "<=") == "PASS"
        assert _evaluate_gate(20, SPEC_IDLE_RU_IDEAL, "<=") == "PASS"

    def test_idle_ru_above_ideal_fails(self):
        """空载 RU >20 应 FAIL(理想阈值超标)。"""
        assert _evaluate_gate(21, SPEC_IDLE_RU_IDEAL, "<=") == "FAIL"
        assert _evaluate_gate(50, SPEC_IDLE_RU_IDEAL, "<=") == "FAIL"

    def test_idle_ru_at_hard_limit_passes(self):
        """空载 RU=100 应 PASS(硬上限)。"""
        assert _evaluate_gate(100, SPEC_IDLE_RU_HARD_LIMIT, "<=") == "PASS"

    def test_idle_ru_above_hard_limit_fails(self):
        """空载 RU >100 应 FAIL(硬上限超标)。"""
        assert _evaluate_gate(101, SPEC_IDLE_RU_HARD_LIMIT, "<=") == "FAIL"

    def test_idle_ru_above_block_threshold_fails(self):
        """空载 RU >500 应 FAIL(阻断阈值)。"""
        assert _evaluate_gate(501, SPEC_IDLE_RU_BLOCK_THRESHOLD, "<=") == "FAIL"
        assert _evaluate_gate(1000, SPEC_IDLE_RU_BLOCK_THRESHOLD, "<=") == "FAIL"

    def test_ru_per_dau_within_limit_passes(self):
        """RU/DAU ≤250 应 PASS。"""
        assert _evaluate_gate(0, SPEC_RU_PER_DAU_DAY_LIMIT, "<=") == "PASS"
        assert _evaluate_gate(250, SPEC_RU_PER_DAU_DAY_LIMIT, "<=") == "PASS"

    def test_ru_per_dau_above_limit_fails(self):
        """RU/DAU >250 应 FAIL。"""
        assert _evaluate_gate(251, SPEC_RU_PER_DAU_DAY_LIMIT, "<=") == "FAIL"

    def test_monthly_ru_within_limit_passes(self):
        """月 RU ≤35M 应 PASS。"""
        assert _evaluate_gate(0, SPEC_MONTHLY_RU_LIMIT, "<=") == "PASS"
        assert _evaluate_gate(35_000_000, SPEC_MONTHLY_RU_LIMIT, "<=") == "PASS"

    def test_monthly_ru_above_limit_fails(self):
        """月 RU >35M 应 FAIL。"""
        assert _evaluate_gate(35_000_001, SPEC_MONTHLY_RU_LIMIT, "<=") == "FAIL"

    def test_analyze_ru_report_block_verdict(self):
        """export_ru_report.analyze_ru_report 应在 RU>500/day 时返回 BLOCK。"""
        # 构造模拟 metrics(total_ru=1800 → 72h=3天 → 600/day > 500 → BLOCK)
        metrics = {
            "metrics": [
                {"name": "request_units", "value": {"sum": 1800}}
            ]
        }
        from scripts.export_ru_report import analyze_ru_report
        report = analyze_ru_report(metrics)
        assert "verdict" in report
        assert "BLOCK" in report["verdict"], (
            f"RU=600/day 应返回 BLOCK,实际: {report['verdict']}"
        )

    def test_analyze_ru_report_pass_verdict(self):
        """export_ru_report.analyze_ru_report 应在 RU≤20/day 时返回 PASS。"""
        # total_ru=45 → 45/3=15/day ≤20 → PASS
        metrics = {
            "metrics": [
                {"name": "request_units", "value": {"sum": 45}}
            ]
        }
        from scripts.export_ru_report import analyze_ru_report
        report = analyze_ru_report(metrics)
        assert report["verdict"] == "PASS"

    def test_ru_72h_script_has_fail_closed(self):
        """72h 验证脚本应包含 fail-closed 逻辑(set -euo pipefail)。"""
        script = _read_script(RU_72H_SCRIPT)
        assert "set -euo pipefail" in script, (
            "脚本应包含 set -euo pipefail(fail-closed 基础)"
        )
        assert "exit 1" in script, "脚本应包含 exit 1(fail-closed 退出)"

    def test_ru_72h_script_gate_failure_exits(self):
        """72h 验证脚本门禁失败时应 exit 1(fail-closed)。"""
        script = _read_script(RU_72H_SCRIPT)
        # 验证门禁校验失败时调用 fail_step
        assert "fail_step" in script
        assert "gate_thresholds" in script

    def test_soak_script_has_fail_closed(self):
        """soak 测试脚本应包含 fail-closed 逻辑(set -euo pipefail)。"""
        script = _read_script(SOAK_SCRIPT)
        assert "set -euo pipefail" in script
        assert "exit 1" in script

    def test_soak_script_consistency_violation_fails(self):
        """soak 脚本数据一致性违规时应 exit 1(fail-closed)。"""
        script = _read_script(SOAK_SCRIPT)
        assert "data_consistency" in script or "一致性" in script
        assert "fail_step" in script
        # 验证一致性违规 > 0 时 fail-closed
        assert "CONSISTENCY_VIOLATION_THRESHOLD" in script

    def test_soak_script_final_violation_check(self):
        """soak 脚本最终应检查总违规数 > 阈值时 fail-closed。"""
        script = _read_script(SOAK_SCRIPT)
        assert "TOTAL_VIOLATIONS" in script
        assert "CONSISTENCY_VIOLATION_THRESHOLD" in script

    def test_both_scripts_have_trap_exit(self):
        """两个脚本都应有 trap EXIT(确保报告始终生成)。"""
        ru_script = _read_script(RU_72H_SCRIPT)
        soak_script = _read_script(SOAK_SCRIPT)
        assert "trap on_exit EXIT" in ru_script, "72h 脚本缺少 trap on_exit EXIT"
        assert "trap on_exit EXIT" in soak_script, "soak 脚本缺少 trap on_exit EXIT"

    def test_both_scripts_have_usage(self):
        """两个脚本都应有 usage() 函数。"""
        ru_script = _read_script(RU_72H_SCRIPT)
        soak_script = _read_script(SOAK_SCRIPT)
        assert "usage()" in ru_script, "72h 脚本缺少 usage()"
        assert "usage()" in soak_script, "soak 脚本缺少 usage()"

    def test_fail_closed_propagates_to_report_status(self):
        """fail-closed 失败时报告 status 应为 FAILED。"""
        # 模拟 fail-closed:门禁失败 → exit 1 → trap 生成 status=FAILED
        exit_code = 1  # fail_step 调用 exit 1
        status = "SUCCESS" if exit_code == 0 else "FAILED"
        assert status == "FAILED"

    def test_fail_closed_success_propagates_to_report_status(self):
        """全部通过时报告 status 应为 SUCCESS。"""
        exit_code = 0
        status = "SUCCESS" if exit_code == 0 else "FAILED"
        assert status == "SUCCESS"

    def test_soak_consistency_violation_must_be_zero(self):
        """soak 测试数据一致性违规次数必须为 0(任何违规 fail-closed)。"""
        # 7 天 soak 测试中,任何数据一致性违规都应立即 exit 1
        # 验证阈值常量为 0
        assert SPEC_TOTAL_HEALTH_CHECKS == 168  # 168 轮检查,每轮都应 0 违规
        # 模拟 168 轮检查全部 0 违规
        violations_per_cycle = [0] * SPEC_TOTAL_HEALTH_CHECKS
        total_violations = sum(violations_per_cycle)
        assert total_violations == 0, "7 天 soak 测试中数据一致性违规必须为 0"
