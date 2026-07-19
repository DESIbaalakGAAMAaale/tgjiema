#!/usr/bin/env python3
"""R65 P1-02 / R64 P1-09: a11y 强制矩阵门禁 — 真实执行对等 + case→test 映射。

审计 R65 P1-02 整改要求:
    A11y 64 矩阵是清单,不是 64 个已执行测试。scanner 默认调用不带
    ``--test-report``;代码明确在无报告时返回 ``ok=True`` 并输出
    "跳过执行对等检查"。因此 CI 中 Scanner 9 只验证 generated JSON 的
    笛卡尔积,不验证 Playwright 实际执行数、skip 或失败。

整改:
    1. ``--test-report`` 改为强制必填(无报告即失败)。
    2. 严格解析 Playwright JSON report:
       - ``executed = passed + failed + flaky + timedOut + unexpected``(排除 skipped)
       - 必须 ``executed == 64`` 且 ``passed == 64``
       - ``skipped == 0``(不允许跳过)
       - ``failed/flaky/timedOut/unexpected`` 任一 > 0 即失败
    3. generated case 必须映射到真实 Playwright 测试函数(按命名约定)。

历史背景(R64 P1-09):
    - 矩阵覆盖: locales (zh-CN, en-US) × input_modes (keyboard, screen_reader)
      × states (16 个) = 64 个用例
    - expected_test_count == executed_test_count
    - 任何 skip / xpass 视为失败

CI 调用方式:
    # 严格模式(必填 --test-report)
    python scripts/check_a11y_matrix_enforcement.py \\
        --test-report tests/e2e/test-results/a11y-report.json

退出码:
    0: 矩阵完整 + 执行对等(executed==64, passed==64, skipped==0)+ case→test 映射完整
    1: 任一检查失败(详细原因输出到 stderr)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

# 复用 check_a11y_precheck 的常量与函数(避免重复定义)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_a11y_precheck as precheck  # type: ignore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_FILE = precheck.MATRIX_CASES_JSON

# R65 P1-02: a11y stub 测试目录(每个矩阵用例对应一个 .spec.ts)
A11Y_TESTS_DIR = REPO_ROOT / "tests" / "a11y"

# Playwright JSON report 中需要识别的状态(与 Playwright 内部一致)
# - passed / failed / flaky / timedOut / interrupted: 已执行
# - skipped: 未执行(矩阵禁止)
# - unexpected: Playwright retry 后仍非 pass(已执行但状态异常)
PLAYWRIGHT_EXECUTED_STATUSES: tuple[str, ...] = (
    "passed", "failed", "flaky", "timedOut",
)
# unexpected 不在 results[].status 中,而是 stats.unexpected;此处仅用于文档说明


class EnforcementResult(NamedTuple):
    """门禁结果。"""

    ok: bool
    message: str
    details: dict


# ════════════════════════════════════════════════════════════════
# 检查 1: 矩阵完整性(委托 check_a11y_precheck.check_matrix_completeness)
# ════════════════════════════════════════════════════════════════


def enforce_matrix_completeness(
    matrix_path: Optional[Path] = None,
) -> EnforcementResult:
    """校验矩阵用例文件完整性(expected_count + 矩阵覆盖)。

    Args:
        matrix_path: 矩阵文件路径(默认 MATRIX_FILE)

    Returns:
        EnforcementResult: ok=True 表示矩阵完整
    """
    cr = precheck.check_matrix_completeness(cases_json=matrix_path)
    return EnforcementResult(ok=cr.ok, message=cr.message, details=cr.details)


# ════════════════════════════════════════════════════════════════
# 检查 2: 执行对等(executed == 64, passed == 64, skipped == 0)
# ════════════════════════════════════════════════════════════════


def _parse_playwright_json_report(report_path: Path) -> dict[str, int]:
    """解析 Playwright JSON 报告,按状态分类计数。

    Playwright JSON reporter 输出格式(简化):
        {
            "stats": {
                "expected": 64, "skipped": 0, "unexpected": 0,
                "flaky": 0, "timedOut": 0, "passed": 64, "failed": 0
            },
            "suites": [
                {"specs": [{"tests": [{"results": [{"status": "passed"}]}],
                         "file": "a11y/error_en_us_keyboard.spec.ts"}]}
            ]
        }

    R65 P1-02 严格规则:
        - ``executed = passed + failed + flaky + timedOut + unexpected``(排除 skipped)
        - failed/flaky/timedOut/unexpected 任一 > 0 即视为执行不通过
        - skipped == 0(不允许跳过矩阵用例)

    R65 P1-02 (testDir='..' 适配):
        - Playwright testDir='..' 同时运行 tests/e2e/ + tests/a11y/ 测试
        - 报告包含所有测试(admin/session/mfa + 64 矩阵 stub)
        - 本函数**仅统计 tests/a11y/ 下的矩阵 stub 测试**(通过 spec.file 过滤)
        - 顶层 stats 不再可信(包含非矩阵测试),完全以 suites 遍历为准

    Returns:
        {"passed", "failed", "flaky", "timedOut", "skipped",
         "unexpected", "executed", "expected", "report_parsed"}
        报告不可解析时,report_parsed=False,其余字段为 0。
    """
    result: dict[str, int] = {
        "passed": 0,
        "failed": 0,
        "flaky": 0,
        "timedOut": 0,
        "skipped": 0,
        "unexpected": 0,
        "executed": 0,
        "expected": 0,
        "report_parsed": 0,
    }
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return result

    if not isinstance(data, dict):
        return result

    # R65 P1-02: testDir='..' 后,顶层 stats 包含所有测试(admin/session/mfa/a11y)
    # 不再可信,完全以 suites 遍历(过滤 a11y/)为准。stats 仅作为 expected 参考。
    stats = data.get("stats")
    if isinstance(stats, dict):
        result["expected"] = int(stats.get("expected", 0) or 0)

    # 遍历 suites 统计每个 test 的最终 status(回填/校验 stats)
    # - 每个 spec.tests[] 是一个测试用例,其 results[] 是多次运行(retry)
    # - 最终状态以最后一个 result 为准(与 Playwright 输出一致)
    # R65 P1-02: 仅统计 spec.file 包含 'a11y/' 的矩阵 stub 测试
    #   (testDir='..' 同时运行 tests/e2e/ + tests/a11y/,需过滤非矩阵测试)
    suite_passed = 0
    suite_failed = 0
    suite_flaky = 0
    suite_timed_out = 0
    suite_skipped = 0
    matrix_test_count = 0  # 仅 a11y/ 下的测试数(用于 expected 修正)

    def _is_matrix_spec(spec: dict) -> bool:
        """判断 spec 是否属于 tests/a11y/ 矩阵 stub 测试。

        Playwright JSON report 中 spec.file 是相对 testDir 的路径。
        testDir='..' (tests/),所以:
          - tests/a11y/error_en_us_keyboard.spec.ts → file='a11y/error_en_us_keyboard.spec.ts'
          - tests/e2e/accessibility.spec.ts → file='e2e/accessibility.spec.ts'
        本函数仅匹配 file 包含 'a11y/' 的 spec。
        """
        spec_file = spec.get("file", "") or ""
        # 兼容多种路径格式: 'a11y/...', './a11y/...', '/a11y/...'
        return "a11y/" in spec_file or spec_file.startswith("a11y")

    def _walk_suites(suites: list) -> None:
        nonlocal suite_passed, suite_failed, suite_flaky
        nonlocal suite_timed_out, suite_skipped, matrix_test_count
        for suite in suites:
            if not isinstance(suite, dict):
                continue
            _walk_suites(suite.get("suites", []) or [])
            for spec in suite.get("specs", []) or []:
                if not isinstance(spec, dict):
                    continue
                # R65 P1-02: 仅统计 tests/a11y/ 下的矩阵 stub 测试
                if not _is_matrix_spec(spec):
                    continue
                for test in spec.get("tests", []) or []:
                    if not isinstance(test, dict):
                        continue
                    matrix_test_count += 1
                    results = test.get("results", []) or []
                    if not results:
                        # 无 results 视为 skipped(test 未真正执行)
                        suite_skipped += 1
                        continue
                    # 取最后一次 retry 的状态作为最终状态
                    final_result = results[-1]
                    if not isinstance(final_result, dict):
                        continue
                    status = final_result.get("status", "")
                    # Playwright status enum:
                    #   passed / failed / timedOut / interrupted / skipped / flaky
                    if status == "passed":
                        suite_passed += 1
                    elif status == "failed":
                        suite_failed += 1
                    elif status == "flaky":
                        suite_flaky += 1
                    elif status == "timedOut":
                        suite_timed_out += 1
                    elif status == "skipped":
                        suite_skipped += 1
                    elif status == "interrupted":
                        # interrupted 视为失败(执行被强制中断)
                        suite_failed += 1
                    else:
                        # 未知状态视为已执行但失败(fail-closed)
                        suite_failed += 1

    if isinstance(data.get("suites"), list):
        _walk_suites(data["suites"])

    # R65 P1-02: 完全使用 suites 遍历的精确计数(已过滤 a11y/ 矩阵测试)
    # 不再使用顶层 stats(包含非矩阵测试,数值不匹配 expected=64)
    result["passed"] = suite_passed
    result["failed"] = suite_failed
    result["flaky"] = suite_flaky
    result["timedOut"] = suite_timed_out
    result["skipped"] = suite_skipped
    result["unexpected"] = 0  # unexpected 在 suites 遍历中已分类为 failed
    # expected 修正为实际矩阵测试数(应 == 64)
    result["expected"] = matrix_test_count

    # executed = passed + failed + flaky + timedOut + unexpected(排除 skipped)
    result["executed"] = (
        result["passed"]
        + result["failed"]
        + result["flaky"]
        + result["timedOut"]
        + result["unexpected"]
    )
    result["report_parsed"] = 1
    return result


def enforce_execution_parity(
    test_report: Optional[Path] = None,
    *,
    expected_count: Optional[int] = None,
) -> EnforcementResult:
    """校验 executed == 64 且 passed == 64 且 skipped == 0。

    R65 P1-02 严格规则:
        1. ``--test-report`` 必填,无报告即失败(不再跳过执行对等检查)
        2. ``executed = passed + failed + flaky + timedOut + unexpected``(排除 skipped)
        3. 必须 ``executed == 64`` 且 ``passed == 64``
        4. ``skipped == 0``(不允许跳过矩阵用例)
        5. ``failed/flaky/timedOut/unexpected`` 任一 > 0 即失败

    Args:
        test_report: Playwright JSON 报告路径(必填)
        expected_count: 期望用例数(默认 EXPECTED_MATRIX_CASE_COUNT=64)

    Returns:
        EnforcementResult: ok=True 表示 executed==64, passed==64, skipped==0
    """
    expected = expected_count or precheck.EXPECTED_MATRIX_CASE_COUNT
    details: dict[str, Any] = {
        "expected_count": expected,
        "executed_count": 0,
        "passed": 0,
        "failed": 0,
        "flaky": 0,
        "timedOut": 0,
        "skipped": 0,
        "unexpected": 0,
        "report_parsed": False,
    }

    # R65 P1-02: 无报告直接失败(不再跳过执行对等检查)
    if test_report is None:
        return EnforcementResult(
            ok=False,
            message=(
                "R65 P1-02: Playwright JSON report required (--test-report) — "
                "无报告不得跳过执行对等检查(原 R64 P1-09 旧行为已废弃)"
            ),
            details=details,
        )

    if not test_report.exists():
        return EnforcementResult(
            ok=False,
            message=f"R65 P1-02: 测试报告不存在: {test_report}",
            details=details,
        )

    stats = _parse_playwright_json_report(test_report)
    details.update(stats)
    details["report_parsed"] = bool(stats["report_parsed"])

    if not stats["report_parsed"]:
        return EnforcementResult(
            ok=False,
            message=(
                f"R65 P1-02: 测试报告解析失败(JSON 格式错误或结构无效): "
                f"{test_report}"
            ),
            details=details,
        )

    # 严格逐项检查(顺序: skipped → executed → passed → 失败状态)
    if stats["skipped"] > 0:
        return EnforcementResult(
            ok=False,
            message=(
                f"R65 P1-02: 检测到 {stats['skipped']} 个 skipped 用例"
                f"(不允许跳过矩阵用例,必须 executed==64 且 skipped==0)"
            ),
            details=details,
        )

    if stats["executed"] != expected:
        return EnforcementResult(
            ok=False,
            message=(
                f"R65 P1-02: executed({stats['executed']}) != expected({expected}) — "
                f"passed={stats['passed']}, failed={stats['failed']}, "
                f"flaky={stats['flaky']}, timedOut={stats['timedOut']}, "
                f"unexpected={stats['unexpected']}"
            ),
            details=details,
        )

    if stats["passed"] != expected:
        return EnforcementResult(
            ok=False,
            message=(
                f"R65 P1-02: passed({stats['passed']}) != expected({expected}) — "
                f"必须 64 个用例最终状态全部 passed(不允许 failed/flaky/timedOut/unexpected)"
            ),
            details=details,
        )

    # 任何失败状态(即使 executed==64 + passed==64 边缘场景也要校验)
    failure_total = (
        stats["failed"] + stats["flaky"] + stats["timedOut"] + stats["unexpected"]
    )
    if failure_total > 0:
        return EnforcementResult(
            ok=False,
            message=(
                f"R65 P1-02: 检测到 {failure_total} 个非 passed 状态"
                f"(failed={stats['failed']}, flaky={stats['flaky']}, "
                f"timedOut={stats['timedOut']}, unexpected={stats['unexpected']}) — "
                f"要求 64 个用例最终状态全部 passed"
            ),
            details=details,
        )

    return EnforcementResult(
        ok=True,
        message=(
            f"R65 P1-02: 执行对等检查通过"
            f"(executed={stats['executed']}, passed={stats['passed']}, "
            f"skipped={stats['skipped']}, failed/flaky/timedOut/unexpected=0)"
        ),
        details=details,
    )


# ════════════════════════════════════════════════════════════════
# 检查 3: case → test 映射(每个 generated case 必须有真实 Playwright 测试函数)
# ════════════════════════════════════════════════════════════════


def _normalize_locale_for_filename(locale: str) -> str:
    """将 locale 标识规范化为文件名安全形式。

    规则: 小写 + 连字符转下划线(zh-CN → zh_cn, en-US → en_us)
    """
    return locale.replace("-", "_").lower()


def derive_test_filename(case: dict) -> str:
    """根据矩阵 case 派生对应的 Playwright 测试文件名(命名约定)。

    命名约定: ``{state}_{locale_normalized}_{input_mode}.spec.ts``

    示例:
        case = {state: "error", locale: "zh-CN", input_mode: "keyboard"}
        → "error_zh_cn_keyboard.spec.ts"

        case = {state: "approval_required", locale: "en-US", input_mode: "screen_reader"}
        → "approval_required_en_us_screen_reader.spec.ts"

    Args:
        case: 矩阵用例 dict(必须含 state / locale / input_mode 字段)

    Returns:
        对应的 .spec.ts 文件名(不含路径)
    """
    state = case.get("state", "")
    locale = _normalize_locale_for_filename(case.get("locale", ""))
    input_mode = case.get("input_mode", "")
    return f"{state}_{locale}_{input_mode}.spec.ts"


def _verify_test_file_references(
    file_path: Path,
    case: dict,
) -> tuple[bool, str]:
    """验证测试文件引用了真实 route/state(非纯 JSON 占位)。

    要求文件同时满足:
        1. 包含 ``test(`` 或 ``test.describe(`` 调用(真实 Playwright 测试函数)
        2. 包含 case 的 ``path`` 字段值(路由引用,如 ``/readiness``)

    Args:
        file_path: 测试文件路径
        case: 矩阵用例 dict

    Returns:
        (ok, reason): ok=True 表示引用真实 route/state;否则 reason 描述缺失
    """
    if not file_path.exists():
        return False, f"文件不存在: {file_path}"

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"读取文件失败: {e}"

    # 1. 必须包含 Playwright test 函数定义
    #    test('...') / test.describe('...') / test('...', async ({page}) => { ... })
    if not re.search(r"\btest\s*(?:\.\w+)?\s*\(", content):
        return False, "缺少 Playwright test() 函数定义"

    # 2. 必须引用 case 的 path(路由),证明测试针对真实路由
    case_path = case.get("path", "")
    if case_path and case_path not in content:
        return False, f"未引用 case path '{case_path}'(必须包含路由引用)"

    return True, ""


def enforce_case_to_test_mapping(
    matrix_path: Optional[Path] = None,
    tests_dir: Optional[Path] = None,
) -> EnforcementResult:
    """R65 P1-02: 校验每个 generated case 映射到真实 Playwright 测试函数。

    强制规则:
        1. 矩阵文件必须存在且含 64 个 case
        2. 每个 case 必须有对应的 ``tests/a11y/{state}_{locale}_{input_mode}.spec.ts``
        3. 测试文件必须包含真实 Playwright ``test()`` 函数(非纯 JSON 占位)
        4. 测试文件必须引用 case 的 ``path`` 字段(路由引用)

    Args:
        matrix_path: 矩阵用例文件路径(默认 MATRIX_FILE)
        tests_dir: a11y 测试目录(默认 A11Y_TESTS_DIR)

    Returns:
        EnforcementResult: ok=True 表示所有 case 均映射到真实测试函数
    """
    json_path = matrix_path or MATRIX_FILE
    a11y_dir = tests_dir or A11Y_TESTS_DIR

    details: dict[str, Any] = {
        "total_cases": 0,
        "mapped_cases": 0,
        "missing_test_files": [],
        "invalid_test_files": [],
        "tests_dir": str(a11y_dir),
    }

    if not json_path.exists():
        return EnforcementResult(
            ok=False,
            message=f"R65 P1-02: 矩阵用例文件不存在: {json_path}",
            details=details,
        )

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return EnforcementResult(
            ok=False,
            message=f"R65 P1-02: 矩阵用例文件解析失败: {e}",
            details=details,
        )

    cases = data.get("cases", []) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        return EnforcementResult(
            ok=False,
            message="R65 P1-02: 矩阵用例文件 cases 字段应为数组",
            details=details,
        )

    details["total_cases"] = len(cases)
    if not cases:
        return EnforcementResult(
            ok=False,
            message="R65 P1-02: 矩阵用例文件为空(0 个 case)",
            details=details,
        )

    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            details["missing_test_files"].append(
                f"case[{idx}] 非法 dict: {type(case).__name__}"
            )
            continue

        expected_filename = derive_test_filename(case)
        expected_path = a11y_dir / expected_filename

        if not expected_path.exists():
            details["missing_test_files"].append(
                f"case {expected_filename[:-9]} (idx={idx}, "
                f"state={case.get('state')}, locale={case.get('locale')}, "
                f"input_mode={case.get('input_mode')}) "
                f"无 Playwright 测试函数: {expected_path}"
            )
            continue

        ok, reason = _verify_test_file_references(expected_path, case)
        if not ok:
            details["invalid_test_files"].append(
                f"{expected_filename}: {reason}"
            )
            continue

        details["mapped_cases"] += 1

    if details["missing_test_files"] or details["invalid_test_files"]:
        total_fail = (
            len(details["missing_test_files"]) + len(details["invalid_test_files"])
        )
        return EnforcementResult(
            ok=False,
            message=(
                f"R65 P1-02: case→test 映射不完整 — "
                f"{total_fail}/{details['total_cases']} 个 case 缺失或无效测试函数"
                f"(missing={len(details['missing_test_files'])}, "
                f"invalid={len(details['invalid_test_files'])})"
            ),
            details=details,
        )

    return EnforcementResult(
        ok=True,
        message=(
            f"R65 P1-02: case→test 映射完整"
            f"({details['mapped_cases']}/{details['total_cases']} 个 case "
            f"均映射到真实 Playwright 测试函数,目录: {a11y_dir})"
        ),
        details=details,
    )


# ════════════════════════════════════════════════════════════════
# 主检查流程
# ════════════════════════════════════════════════════════════════


def check(
    matrix_path: Optional[Path] = None,
    test_report: Optional[Path] = None,
    tests_dir: Optional[Path] = None,
) -> tuple[int, list[EnforcementResult]]:
    """主校验流程。

    Args:
        matrix_path: 矩阵文件路径(默认 MATRIX_FILE)
        test_report: Playwright JSON 报告路径(R65 P1-02: 必填)
        tests_dir: a11y 测试目录(默认 A11Y_TESTS_DIR)

    Returns:
        (exit_code, results)
        exit_code: 0=全部通过, 1=任一失败
        results: 所有检查结果列表
    """
    results: list[EnforcementResult] = []

    # 检查 1: 矩阵完整性
    results.append(enforce_matrix_completeness(matrix_path))

    # 检查 2: 执行对等(R65 P1-02: test_report 必填,无报告即失败)
    results.append(enforce_execution_parity(test_report))

    # 检查 3: case → test 映射
    results.append(enforce_case_to_test_mapping(matrix_path, tests_dir))

    exit_code = 0 if all(r.ok for r in results) else 1
    return exit_code, results


def main() -> int:
    """CLI 主入口。返回 0(全部通过)或 1(任一失败)。"""
    parser = argparse.ArgumentParser(
        description=(
            "R65 P1-02 / R64 P1-09: a11y 强制矩阵门禁"
            "(真实执行对等: executed==64, passed==64, skipped==0;"
            "case→test 映射完整)"
        ),
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=None,
        help=f"矩阵用例文件路径(默认: {MATRIX_FILE})",
    )
    parser.add_argument(
        "--test-report",
        type=Path,
        required=True,
        help=(
            "Playwright JSON 测试报告路径(必填) — "
            "R65 P1-02: 无报告即失败(原 R64 P1-09 跳过行为已废弃)"
        ),
    )
    parser.add_argument(
        "--a11y-tests-dir",
        type=Path,
        default=None,
        help=f"a11y 测试目录(默认: {A11Y_TESTS_DIR})",
    )
    args = parser.parse_args()

    exit_code, results = check(
        matrix_path=args.matrix,
        test_report=args.test_report,
        tests_dir=args.a11y_tests_dir,
    )

    # 输出结果
    for r in results:
        stream = sys.stdout if r.ok else sys.stderr
        prefix = "[PASS]" if r.ok else "[FAIL]"
        print(f"{prefix} {r.message}", file=stream)
        if r.details:
            for k, v in r.details.items():
                if v or isinstance(v, bool):
                    print(f"       {k}: {v}", file=stream)

    if exit_code != 0:
        print(
            f"\n[R65 P1-02] 矩阵门禁失败: "
            f"{sum(1 for r in results if not r.ok)}/{len(results)} 项未通过",
            file=sys.stderr,
        )
        return 1
    print(f"\n[R65 P1-02] 矩阵门禁通过({len(results)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
