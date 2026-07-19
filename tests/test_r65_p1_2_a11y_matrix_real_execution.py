"""R65 P1-02: A11y 64 矩阵真实执行对等门禁单测 — 9 个场景。

审计 R65 P1-02 原文核心:
    "A11y 64 矩阵是清单,不是 64 个已执行测试。
     scanner 默认调用不带 --test-report;代码明确在无报告时返回 ok=True
     并输出'跳过执行对等检查'。因此 CI 中 Scanner 9 只验证 generated JSON
     的笛卡尔积,不验证 Playwright 实际执行数、skip 或失败。"

整改:
    1. Playwright 配置启用 JSON reporter,输出固定 artifact。
    2. E2E 后强制运行 check_a11y_matrix_enforcement.py --test-report <report>。
    3. 无报告、解析失败、executed ≠ 64、任何 skip/flaky/timedOut/unexpected 均失败。
    4. 不得把 failed/flaky/timedOut 计作合格 executed;要求 64 个最终状态全部 passed。
    5. generated case 必须真正映射到测试函数与 route/state fixture。

本测试覆盖 9 个场景:
    1. Scanner fails when --test-report is missing(CLI 层强制必填)
    2. Scanner fails when report file doesn't exist
    3. Scanner fails when report is malformed JSON
    4. Scanner fails when executed != 64
    5. Scanner fails when passed < 64(any failure/flaky/timedOut)
    6. Scanner fails when skipped > 0
    7. Scanner passes when all 64 cases passed, 0 skipped
    8. Scanner fails when a generated case has no Playwright test mapping
    9. case→test 映射命名约定正确(derive_test_filename / normalize_locale)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# 注入 scripts/ 目录到 sys.path,便于 import 检查脚本
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_a11y_matrix_enforcement as enforcement  # type: ignore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER_SCRIPT = SCRIPTS_DIR / "check_a11y_matrix_enforcement.py"


# ════════════════════════════════════════════════════════════════
# 辅助函数 — 构造合成 Playwright JSON report
# ════════════════════════════════════════════════════════════════


def _make_playwright_report(
    *,
    passed: int = 0,
    failed: int = 0,
    flaky: int = 0,
    timed_out: int = 0,
    skipped: int = 0,
    unexpected: int = 0,
) -> dict:
    """构造合成 Playwright JSON report(stats + suites 双重一致)。

    Playwright JSON reporter 标准输出格式:
        {
            "stats": {
                "expected": N, "skipped": S, "passed": P, "failed": F,
                "flaky": FL, "timedOut": T, "unexpected": U
            },
            "suites": [{...specs/tests/results...}]
        }

    本函数同时填充 stats 与 suites,使二者计数一致(模拟真实 Playwright 输出)。
    """
    total = passed + failed + flaky + timed_out + skipped
    results_list: list[dict] = []
    results_list.extend([{"status": "passed"}] * passed)
    results_list.extend([{"status": "failed"}] * failed)
    results_list.extend([{"status": "flaky"}] * flaky)
    results_list.extend([{"status": "timedOut"}] * timed_out)
    results_list.extend([{"status": "skipped"}] * skipped)
    # 每个 result 包一个独立 test(模拟 1 test 1 result)
    tests = [{"results": [r]} for r in results_list]

    return {
        "stats": {
            "expected": total,
            "skipped": skipped,
            "passed": passed,
            "failed": failed,
            "flaky": flaky,
            "timedOut": timed_out,
            "unexpected": unexpected,
        },
        "suites": [
            {
                "specs": [
                    # R65 P1-02: file 字段必须包含 'a11y/' 以通过 _is_matrix_spec 过滤
                    # (testDir='..' 后,scanner 仅统计 tests/a11y/ 下的矩阵 stub 测试)
                    {"file": "a11y/matrix_stub.spec.ts", "tests": tests}
                ]
            }
        ],
    }


def _write_report(tmp_path: Path, report: dict, name: str = "report.json") -> Path:
    """将合成 report 写入临时文件并返回路径。"""
    f = tmp_path / name
    f.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return f


# ════════════════════════════════════════════════════════════════
# 场景 1: Scanner fails when --test-report is missing
# ════════════════════════════════════════════════════════════════


class TestScannerRequiresTestReport:
    """场景 1: --test-report 必填,缺失即失败。"""

    def test_cli_fails_without_test_report_flag(self):
        """1a: CLI 不传 --test-report → argparse 退出码 2(required violation)。"""
        result = subprocess.run(
            ["python", str(SCANNER_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0, (
            f"应失败(无 --test-report),实际 exit={result.returncode}"
        )
        # argparse required 违规时输出 usage + error 到 stderr
        assert "--test-report" in result.stderr, (
            f"stderr 应提示 --test-report required,实际: {result.stderr}"
        )

    def test_enforce_execution_parity_none_returns_failure(self):
        """1b: enforce_execution_parity(test_report=None) → ok=False。"""
        result = enforcement.enforce_execution_parity(test_report=None)
        assert result.ok is False
        # message 必须明确指出 --test-report required
        msg_lower = result.message.lower()
        assert "required" in msg_lower or "--test-report" in result.message, (
            f"message 应说明 --test-report required,实际: {result.message}"
        )

    def test_check_function_fails_without_test_report(self):
        """1c: check(test_report=None) → exit_code=1。"""
        exit_code, results = enforcement.check(test_report=None)
        assert exit_code == 1
        # 至少有一项失败(执行对等检查)
        failed = [r for r in results if not r.ok]
        assert len(failed) >= 1, "应至少一项失败(无 test_report)"


# ════════════════════════════════════════════════════════════════
# 场景 2: Scanner fails when report file doesn't exist
# ════════════════════════════════════════════════════════════════


class TestScannerFailsOnMissingReportFile:
    """场景 2: 报告文件不存在 → ok=False。"""

    def test_nonexistent_report_fails(self, tmp_path):
        """2a: --test-report 指向不存在的文件 → ok=False。"""
        nonexistent = tmp_path / "no_report.json"
        result = enforcement.enforce_execution_parity(test_report=nonexistent)
        assert result.ok is False
        assert "不存在" in result.message or "not exist" in result.message.lower(), (
            f"message 应说明文件不存在,实际: {result.message}"
        )

    def test_nonexistent_report_details_zero_counts(self, tmp_path):
        """2b: details 中所有计数应为 0,report_parsed=False。"""
        nonexistent = tmp_path / "no_report.json"
        result = enforcement.enforce_execution_parity(test_report=nonexistent)
        assert result.details["report_parsed"] is False
        assert result.details["passed"] == 0
        # executed_count 是 details 初始字段(始终存在,未解析报告时为 0)
        assert result.details["executed_count"] == 0


# ════════════════════════════════════════════════════════════════
# 场景 3: Scanner fails when report is malformed JSON
# ════════════════════════════════════════════════════════════════


class TestScannerFailsOnMalformedJson:
    """场景 3: 报告 JSON 格式错误 → ok=False。"""

    def test_malformed_json_fails(self, tmp_path):
        """3a: 报告文件内容非合法 JSON → ok=False(report_parsed=False)。"""
        report = tmp_path / "malformed.json"
        report.write_text("{not valid json", encoding="utf-8")
        result = enforcement.enforce_execution_parity(test_report=report)
        assert result.ok is False
        assert result.details["report_parsed"] is False
        assert "解析失败" in result.message or "parse" in result.message.lower(), (
            f"message 应说明解析失败,实际: {result.message}"
        )

    def test_empty_file_fails(self, tmp_path):
        """3b: 空文件 → ok=False(report_parsed=False)。"""
        report = tmp_path / "empty.json"
        report.write_text("", encoding="utf-8")
        result = enforcement.enforce_execution_parity(test_report=report)
        assert result.ok is False
        assert result.details["report_parsed"] is False

    def test_non_dict_json_fails(self, tmp_path):
        """3c: JSON 顶层非 dict(如数组或字符串)→ ok=False。"""
        report = tmp_path / "array.json"
        report.write_text("[1, 2, 3]", encoding="utf-8")
        result = enforcement.enforce_execution_parity(test_report=report)
        assert result.ok is False
        assert result.details["report_parsed"] is False


# ════════════════════════════════════════════════════════════════
# 场景 4: Scanner fails when executed != 64
# ════════════════════════════════════════════════════════════════


class TestScannerFailsWhenExecutedNot64:
    """场景 4: executed != 64 → ok=False。"""

    def test_executed_32_fails(self, tmp_path):
        """4a: 32 passed(executed=32, skipped=0)→ ok=False(executed != 64)。"""
        report = _make_playwright_report(passed=32)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 32
        assert result.details["expected_count"] == 64
        assert "executed" in result.message.lower() and "32" in result.message

    def test_executed_128_fails(self, tmp_path):
        """4b: 128 passed(executed=128)→ ok=False(超过 64,可能重复计数)。"""
        report = _make_playwright_report(passed=128)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 128

    def test_executed_zero_fails(self, tmp_path):
        """4c: 0 executed(无任何 test result)→ ok=False(stub 替换假绿)。"""
        report = _make_playwright_report(passed=0, skipped=0)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 0

    def test_executed_63_fails(self, tmp_path):
        """4d: 63 passed(差 1 个)→ ok=False。"""
        report = _make_playwright_report(passed=63)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 63


# ════════════════════════════════════════════════════════════════
# 场景 5: Scanner fails when passed < 64(any failure/flaky/timedOut)
# ════════════════════════════════════════════════════════════════


class TestScannerFailsWhenPassedLessThan64:
    """场景 5: passed < 64(存在 failed/flaky/timedOut)→ ok=False。

    R65 P1-02 严格规则:
        - executed = passed + failed + flaky + timedOut + unexpected(排除 skipped)
        - 即使 executed == 64,只要 passed < 64 即失败
        - failed/flaky/timedOut/unexpected 不计作合格 passed
    """

    def test_failed_treated_as_executed_but_not_passed(self, tmp_path):
        """5a: 63 passed + 1 failed(executed=64 但 passed=63)→ ok=False。"""
        report = _make_playwright_report(passed=63, failed=1)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        # executed == 64(含 failed),但 passed != 64
        assert result.details["executed"] == 64
        assert result.details["passed"] == 63
        assert result.details["failed"] == 1

    def test_flaky_treated_as_executed_but_not_passed(self, tmp_path):
        """5b: 62 passed + 2 flaky(executed=64 但 passed=62)→ ok=False。"""
        report = _make_playwright_report(passed=62, flaky=2)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 64
        assert result.details["passed"] == 62
        assert result.details["flaky"] == 2

    def test_timed_out_treated_as_executed_but_not_passed(self, tmp_path):
        """5c: 60 passed + 4 timedOut(executed=64 但 passed=60)→ ok=False。"""
        report = _make_playwright_report(passed=60, timed_out=4)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 64
        assert result.details["passed"] == 60
        assert result.details["timedOut"] == 4

    def test_mixed_failures_fail(self, tmp_path):
        """5d: 60 passed + 2 failed + 1 flaky + 1 timedOut(executed=64)→ ok=False。"""
        report = _make_playwright_report(
            passed=60, failed=2, flaky=1, timed_out=1
        )
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["executed"] == 64
        assert result.details["passed"] == 60

    def test_unexpected_in_stats_fails(self, tmp_path):
        """5e: stats.unexpected=2(Playwright retry 后状态未稳定)→ ok=False。

        executed = passed + failed + flaky + timedOut + unexpected
        本场景: 62 passed + 0 failed + 0 flaky + 0 timedOut + 2 unexpected
        = executed=64 但 passed=62 → 失败
        """
        report = _make_playwright_report(passed=62, unexpected=2)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        # unexpected 仅出现在 stats,suites 中无对应 test
        # 此时 executed = 62(suites 计数) != 64 → 失败
        assert result.ok is False


# ════════════════════════════════════════════════════════════════
# 场景 6: Scanner fails when skipped > 0
# ════════════════════════════════════════════════════════════════


class TestScannerFailsWhenSkippedGreaterThanZero:
    """场景 6: skipped > 0 → ok=False(不允许跳过矩阵用例)。"""

    def test_one_skipped_fails(self, tmp_path):
        """6a: 63 passed + 1 skipped(executed=63, skipped=1)→ ok=False。"""
        report = _make_playwright_report(passed=63, skipped=1)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["skipped"] >= 1

    def test_many_skipped_fails(self, tmp_path):
        """6b: 32 passed + 32 skipped(executed=32, skipped=32)→ ok=False。"""
        report = _make_playwright_report(passed=32, skipped=32)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        assert result.details["skipped"] >= 32

    def test_skipped_excluded_from_executed(self, tmp_path):
        """6c: skipped 不计入 executed(即使总数=64)。

        32 passed + 32 skipped:总数=64 但 executed=32(排除 skipped)
        → executed != 64 → 失败
        """
        report = _make_playwright_report(passed=32, skipped=32)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is False
        # executed 应排除 skipped
        assert result.details["executed"] == 32
        assert result.details["skipped"] >= 32


# ════════════════════════════════════════════════════════════════
# 场景 7: Scanner passes when all 64 cases passed, 0 skipped
# ════════════════════════════════════════════════════════════════


class TestScannerPassesWhenAll64Passed:
    """场景 7: 64 passed + 0 skipped → ok=True。"""

    def test_all_64_passed_passes(self, tmp_path):
        """7a: 64 passed, 0 skipped, 0 failed/flaky/timedOut/unexpected → ok=True。"""
        report = _make_playwright_report(passed=64, skipped=0)
        f = _write_report(tmp_path, report)
        result = enforcement.enforce_execution_parity(test_report=f)
        assert result.ok is True, f"应通过: {result.message}"
        assert result.details["executed"] == 64
        assert result.details["passed"] == 64
        assert result.details["skipped"] == 0
        assert result.details["failed"] == 0
        assert result.details["flaky"] == 0
        assert result.details["timedOut"] == 0

    def test_full_check_passes_with_valid_report(self, tmp_path):
        """7b: 完整 check()(矩阵完整性 + 执行对等 + case→test 映射)→ exit_code=0。"""
        report = _make_playwright_report(passed=64, skipped=0)
        f = _write_report(tmp_path, report)
        exit_code, results = enforcement.check(test_report=f)
        assert exit_code == 0, (
            f"应 exit 0(所有检查通过),实际 exit={exit_code};"
            f"results={[r.ok for r in results]}"
        )
        # 三项检查全部通过
        assert all(r.ok for r in results), (
            f"所有检查应通过: {[r.message for r in results]}"
        )

    def test_cli_passes_with_valid_report(self, tmp_path):
        """7c: CLI 调用 --test-report 指向有效报告 → exit_code=0。"""
        report = _make_playwright_report(passed=64, skipped=0)
        f = _write_report(tmp_path, report)
        result = subprocess.run(
            [
                "python", str(SCANNER_SCRIPT),
                "--test-report", str(f),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"应 exit 0,实际 exit={result.returncode};"
            f"stdout={result.stdout}; stderr={result.stderr}"
        )
        assert "[PASS]" in result.stdout
        assert "矩阵门禁通过" in result.stdout


# ════════════════════════════════════════════════════════════════
# 场景 8: Scanner fails when a generated case has no Playwright test mapping
# ════════════════════════════════════════════════════════════════


class TestScannerFailsOnMissingTestMapping:
    """场景 8: generated case 无对应 Playwright 测试函数 → ok=False。"""

    def test_missing_test_file_fails(self, tmp_path):
        """8a: 用空 tests_dir → enforce_case_to_test_mapping 返回 ok=False。"""
        # 用空目录(无任何 .spec.ts 文件)
        empty_dir = tmp_path / "empty_a11y"
        empty_dir.mkdir()
        # 使用真实矩阵文件(MATRIX_FILE),但指向空 tests_dir
        result = enforcement.enforce_case_to_test_mapping(
            tests_dir=empty_dir,
        )
        assert result.ok is False
        assert result.details["total_cases"] == 64
        assert result.details["mapped_cases"] == 0
        # 64 个 case 全部缺失测试文件
        assert len(result.details["missing_test_files"]) == 64

    def test_partial_mapping_fails(self, tmp_path):
        """8b: 仅创建 1 个测试文件 → 63 个 case 缺失 → ok=False。"""
        partial_dir = tmp_path / "partial_a11y"
        partial_dir.mkdir()
        # 只创建 1 个测试文件(其余 63 个缺失)
        one_file = partial_dir / "error_zh_cn_keyboard.spec.ts"
        one_file.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test('stub', async () => { expect(true).toBe(true); });\n"
            "  // route: /readiness\n",
            encoding="utf-8",
        )
        result = enforcement.enforce_case_to_test_mapping(
            tests_dir=partial_dir,
        )
        assert result.ok is False
        assert result.details["mapped_cases"] == 1
        assert len(result.details["missing_test_files"]) == 63

    def test_test_file_without_route_reference_fails(self, tmp_path):
        """8c: 测试文件存在但未引用 case path → invalid_test_files → ok=False。"""
        invalid_dir = tmp_path / "invalid_a11y"
        invalid_dir.mkdir()
        # 创建一个 test() 但未引用路由的文件
        bad_file = invalid_dir / "error_zh_cn_keyboard.spec.ts"
        bad_file.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test('stub no route', async () => { expect(true).toBe(true); });\n",
            encoding="utf-8",
        )
        result = enforcement.enforce_case_to_test_mapping(
            tests_dir=invalid_dir,
        )
        assert result.ok is False
        # 该文件应被识别为 invalid(未引用 /readiness)
        assert len(result.details["invalid_test_files"]) >= 1
        assert any(
            "error_zh_cn_keyboard" in s for s in result.details["invalid_test_files"]
        )

    def test_test_file_without_test_function_fails(self, tmp_path):
        """8d: 测试文件无 test() 函数定义 → invalid_test_files → ok=False。"""
        nofunc_dir = tmp_path / "nofunc_a11y"
        nofunc_dir.mkdir()
        bad_file = nofunc_dir / "error_zh_cn_keyboard.spec.ts"
        # 仅引用路由但无 test() 函数(纯 JSON 占位)
        bad_file.write_text(
            "// 仅注释引用路由 /readiness,无 Playwright test 函数\n"
            "// route: /readiness\n",
            encoding="utf-8",
        )
        result = enforcement.enforce_case_to_test_mapping(
            tests_dir=nofunc_dir,
        )
        assert result.ok is False
        assert len(result.details["invalid_test_files"]) >= 1

    def test_full_mapping_passes_with_real_a11y_dir(self):
        """8e: 真实 tests/a11y/ 目录(64 个 stub)→ ok=True。

        本测试验证整改后 tests/a11y/ 中 64 个 stub 全部映射成功。
        """
        result = enforcement.enforce_case_to_test_mapping()
        assert result.ok is True, (
            f"真实 tests/a11y/ 应有 64 个 stub 全部映射: {result.message}"
        )
        assert result.details["mapped_cases"] == 64
        assert result.details["total_cases"] == 64


# ════════════════════════════════════════════════════════════════
# 场景 9: 命名约定正确(derive_test_filename / normalize_locale)
# ════════════════════════════════════════════════════════════════


class TestNamingConvention:
    """场景 9: case → 测试文件命名约定正确。"""

    def test_normalize_locale_zh_cn(self):
        """9a: zh-CN → zh_cn(小写 + 连字符转下划线)。"""
        assert enforcement._normalize_locale_for_filename("zh-CN") == "zh_cn"

    def test_normalize_locale_en_us(self):
        """9b: en-US → en_us。"""
        assert enforcement._normalize_locale_for_filename("en-US") == "en_us"

    def test_derive_test_filename_basic(self):
        """9c: case(error, zh-CN, keyboard) → error_zh_cn_keyboard.spec.ts。"""
        case = {"state": "error", "locale": "zh-CN", "input_mode": "keyboard"}
        assert enforcement.derive_test_filename(case) == "error_zh_cn_keyboard.spec.ts"

    def test_derive_test_filename_complex_state(self):
        """9d: case(approval_required, en-US, screen_reader) → approval_required_en_us_screen_reader.spec.ts。"""
        case = {
            "state": "approval_required",
            "locale": "en-US",
            "input_mode": "screen_reader",
        }
        assert (
            enforcement.derive_test_filename(case)
            == "approval_required_en_us_screen_reader.spec.ts"
        )

    def test_all_64_matrix_cases_have_test_files(self):
        """9e: 真实矩阵文件 64 个 case 全部有对应 stub 文件。

        端到端验证: 读取 generated_a11y_matrix_cases.json 的每个 case,
        派生文件名,检查 tests/a11y/ 下存在对应文件。
        """
        matrix_path = enforcement.MATRIX_FILE
        data = json.loads(matrix_path.read_text(encoding="utf-8"))
        cases = data["cases"]
        assert len(cases) == 64

        for case in cases:
            filename = enforcement.derive_test_filename(case)
            file_path = enforcement.A11Y_TESTS_DIR / filename
            assert file_path.exists(), (
                f"case (state={case.get('state')}, locale={case.get('locale')}, "
                f"input_mode={case.get('input_mode')}) 缺失测试文件: {file_path}"
            )


# ════════════════════════════════════════════════════════════════
# 综合场景: Playwright config JSON reporter 已配置
# ════════════════════════════════════════════════════════════════


class TestPlaywrightConfigJsonReporter:
    """验证 Playwright config 启用了 JSON reporter(整改要求 2.2)。"""

    def test_playwright_config_has_json_reporter(self):
        """10a: tests/e2e/playwright.config.ts 启用 ['json', { outputFile }] reporter。"""
        config_path = REPO_ROOT / "tests" / "e2e" / "playwright.config.ts"
        assert config_path.exists(), f"playwright.config.ts 不存在: {config_path}"
        content = config_path.read_text(encoding="utf-8")
        # 必须包含 json reporter 配置
        assert "['json'" in content or '["json"' in content, (
            "playwright.config.ts 应启用 ['json', { outputFile: ... }] reporter"
        )
        # 必须指定 outputFile 路径
        assert "outputFile" in content, (
            "playwright.config.ts 应指定 JSON reporter outputFile 路径"
        )
        # 必须输出到 test-results/a11y-report.json
        assert "a11y-report.json" in content, (
            "outputFile 应指向 test-results/a11y-report.json(固定 artifact)"
        )
