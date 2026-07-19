#!/usr/bin/env python3
"""R64 P1-09: a11y 强制矩阵门禁 — expected==executed,无 skip/xpass,矩阵完整覆盖。

审计 P1-09 强制规则:
    1. expected_test_count 必须等于 executed_test_count
    2. 任何 skip / xpass 视为失败(exit code != 0)
    3. 必须覆盖矩阵: locales (zh-CN, en-US) × input_modes (keyboard, screen_reader)
       × states (16 个: error/loading/empty/paginated/modal/dynamic_button/
                       permission_denied/approval_*/mfa_*) = 64 个用例
    4. 缺失任何维度 → fail-closed

本门禁消费 ``tests/e2e/generated_a11y_matrix_cases.json``(由
``scripts/generate_a11y_matrix_cases.py`` 产出),并可选消费 Playwright
测试报告(JSON 格式)验证 executed_count / skip / xpass。

与 ``check_a11y_precheck.py`` 的关系:
    - check_a11y_precheck.py: R62 P1-06 依赖/用例数/locale/路由元数据预检查
    - check_a11y_matrix_enforcement.py: R64 P1-09 矩阵完整性 + 执行对等门禁
    两者独立运行,CI 中作为不同 Scanner 步骤。

CI 调用方式:
    # 仅校验矩阵文件完整性(无需运行测试)
    python scripts/check_a11y_matrix_enforcement.py
    # 校验矩阵 + 测试报告执行对等
    python scripts/check_a11y_matrix_enforcement.py --test-report tests/e2e/results.json

退出码:
    0: 矩阵完整 + (若提供报告)expected==executed 且 skip/xpass=0
    1: 任一检查失败(详细原因输出到 stderr)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

# 复用 check_a11y_precheck 的常量与函数(避免重复定义)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_a11y_precheck as precheck  # type: ignore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_FILE = precheck.MATRIX_CASES_JSON


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
# 检查 2: 执行对等(expected == executed,无 skip/xpass)
# ════════════════════════════════════════════════════════════════


def _parse_playwright_json_report(report_path: Path) -> dict[str, int]:
    """解析 Playwright JSON 报告,提取 executed/skip/xpass 计数。

    Playwright JSON reporter 输出格式(简化):
        {
            "stats": {"expected": 64, "skipped": 0, ...},
            "suites": [...]
        }
    本函数遍历 suites 中的 spec/test,统计实际执行的用例数与 skip 数。
    xpass 在 Playwright 中无原生概念(fail-fast 模式下 unexpected pass 不计),
    此处按 0 处理(若上游标记 xfail 且通过,Playwright 标记为 flaky,不计 xpass)。

    Returns:
        {"executed": N, "skip": M, "xpass": 0, "expected": E}
        报告不可解析时返回全 0。
    """
    result = {"executed": 0, "skip": 0, "xpass": 0, "expected": 0}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return result

    # 顶层 stats(若存在)
    stats = data.get("stats") if isinstance(data, dict) else None
    if isinstance(stats, dict):
        result["expected"] = int(stats.get("expected", 0))
        # Playwright stats: total / expected / unexpected / flaky / skipped
        result["skip"] = int(stats.get("skipped", 0))

    # 遍历 suites 统计实际执行用例
    def _walk_suites(suites: list) -> None:
        for suite in suites:
            if not isinstance(suite, dict):
                continue
            _walk_suites(suite.get("suites", []) or [])
            for spec in suite.get("specs", []) or []:
                if not isinstance(spec, dict):
                    continue
                for test in spec.get("tests", []) or []:
                    if not isinstance(test, dict):
                        continue
                    for res in test.get("results", []) or []:
                        if not isinstance(res, dict):
                            continue
                        status = res.get("status", "")
                        if status == "skipped":
                            result["skip"] += 1
                        else:
                            # passed / failed / flaky / timedOut 均视为已执行
                            result["executed"] += 1

    if isinstance(data, dict) and isinstance(data.get("suites"), list):
        _walk_suites(data["suites"])

    # 若 stats.expected 为 0 但有执行用例,用 executed 回填
    if result["expected"] == 0 and result["executed"] > 0:
        result["expected"] = result["executed"]
    return result


def enforce_execution_parity(
    test_report: Optional[Path] = None,
    *,
    expected_count: Optional[int] = None,
) -> EnforcementResult:
    """校验 expected == executed 且无 skip/xpass。

    Args:
        test_report: Playwright JSON 报告路径(若提供则解析实际执行数)
        expected_count: 期望用例数(默认 EXPECTED_MATRIX_CASE_COUNT=64)

    Returns:
        EnforcementResult: ok=True 表示 expected==executed 且 skip/xpass=0
    """
    expected = expected_count or precheck.EXPECTED_MATRIX_CASE_COUNT
    details: dict[str, Any] = {
        "expected_count": expected,
        "executed_count": 0,
        "skip_count": 0,
        "xpass_count": 0,
        "report_parsed": False,
    }

    if test_report is None:
        # 无报告时仅校验 expected_count 常量(矩阵文件本身由检查 1 验证)
        return EnforcementResult(
            ok=True,
            message=(
                f"R64 P1-09: 未提供测试报告,跳过执行对等检查"
                f"(expected_count={expected},矩阵文件完整性由检查 1 保证)"
            ),
            details=details,
        )

    if not test_report.exists():
        return EnforcementResult(
            ok=False,
            message=f"R64 P1-09: 测试报告不存在: {test_report}",
            details=details,
        )

    stats = _parse_playwright_json_report(test_report)
    details.update(stats)
    details["report_parsed"] = True

    cr = precheck.check_expected_count_equals_executed(
        executed_count=stats["executed"],
        skip_count=stats["skip"],
        xpass_count=stats["xpass"],
    )
    return EnforcementResult(ok=cr.ok, message=cr.message, details=details)


# ════════════════════════════════════════════════════════════════
# 主检查流程
# ════════════════════════════════════════════════════════════════


def check(
    matrix_path: Optional[Path] = None,
    test_report: Optional[Path] = None,
) -> tuple[int, list[EnforcementResult]]:
    """主校验流程。

    Args:
        matrix_path: 矩阵文件路径(默认 MATRIX_FILE)
        test_report: Playwright JSON 报告路径(可选)

    Returns:
        (exit_code, results)
        exit_code: 0=全部通过, 1=任一失败
        results: 所有检查结果列表
    """
    results: list[EnforcementResult] = []

    # 检查 1: 矩阵完整性
    results.append(enforce_matrix_completeness(matrix_path))

    # 检查 2: 执行对等(仅当提供测试报告时)
    results.append(enforce_execution_parity(test_report))

    exit_code = 0 if all(r.ok for r in results) else 1
    return exit_code, results


def main() -> int:
    """CLI 主入口。返回 0(全部通过)或 1(任一失败)。"""
    parser = argparse.ArgumentParser(
        description="R64 P1-09: a11y 强制矩阵门禁"
        "(expected==executed, 无 skip/xpass, 矩阵 2×2×16 完整覆盖)",
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
        default=None,
        help="Playwright JSON 测试报告路径(可选,用于校验 executed==expected)",
    )
    args = parser.parse_args()

    exit_code, results = check(
        matrix_path=args.matrix,
        test_report=args.test_report,
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
            f"\n[R64 P1-09] 矩阵门禁失败: "
            f"{sum(1 for r in results if not r.ok)}/{len(results)} 项未通过",
            file=sys.stderr,
        )
        return 1
    print(f"\n[R64 P1-09] 矩阵门禁通过({len(results)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
