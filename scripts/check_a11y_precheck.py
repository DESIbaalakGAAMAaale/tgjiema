#!/usr/bin/env python3
"""R62 P1-06: a11y 测试预检查脚本 — 在 E2E 测试运行前硬失败检测假绿条件。

审计 P1-06 整改要求:
    测试文件注释明确:运行时若 Playwright 或 axe 依赖缺失,``test.describe`` 自动 skip。
    对 release gate 来说,这是典型假绿条件。

本脚本在 ``npx playwright test`` 之前运行,验证以下条件(任一失败即 exit 1):

    1. 依赖检查: ``@playwright/test`` 与 ``@axe-core/playwright`` 已安装且可 require。
    2. 用例数检查: ``accessibility_behavior.spec.ts`` 生成的用例数 > 0
       (检测 stub 替换导致的 0 用例 = 绿色 CI 假绿)。
    3. locale 覆盖检查: 测试用例名同时包含 ``zh-CN`` 与 ``en-US`` 标记
       (检测 en-US locale 未被执行,例如 /login en-US 复用 zh-CN 结果)。
    4. 路由元数据检查: ``generated_a11y_cases.json`` 中每条 ``a11y_testable`` 用例
       必须有完整的 ``path`` / ``route_path`` / ``method`` / ``permission`` 字段。

退出码:
    0 — 所有检查通过
    1 — 任一检查失败(详细原因输出到 stderr)

用法:
    python scripts/check_a11y_precheck.py
    python scripts/check_a11y_precheck.py --skip-node-require  # 跳过 node require(本地无 node 时)
    python scripts/check_a11y_precheck.py --test-output <file>  # 额外检查测试输出中的 locale 标记

设计约束:
    - 函数返回 ``CheckResult``(namedtuple),便于 ``tests/test_r62_p1_6_a11y_precheck.py`` 单测。
    - 不依赖 admin / config 模块(纯静态文件 + 子进程检查),避免 import 副作用。
    - 子进程调用使用 ``capture_output=True`` 捕获 stderr,避免污染主进程输出。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = REPO_ROOT / "tests" / "e2e"
SPEC_FILE = E2E_DIR / "accessibility_behavior.spec.ts"
CASES_JSON = E2E_DIR / "generated_a11y_cases.json"

# R62 P1-06: 预期的 locale 标记(测试用例名应同时包含两者)
EXPECTED_LOCALES = ("zh-CN", "en-US")


def _relpath(p: Path) -> str:
    """安全地计算相对 REPO_ROOT 的路径;不在 REPO_ROOT 下时返回绝对路径字符串。

    避免 ``Path.relative_to`` 在路径不在 REPO_ROOT 下时抛 ValueError
    (单测中 SPEC_FILE/CASES_JSON 常被 monkeypatch 到 tmp_path)。
    """
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


class CheckResult(NamedTuple):
    """预检查结果。

    Attributes:
        ok: 检查是否通过
        message: 人类可读的结果描述(通过/失败原因)
        details: 额外细节(如用例数、缺失字段列表),供调试与单测断言使用
    """

    ok: bool
    message: str
    details: dict


# ════════════════════════════════════════════════════════════════
# 检查 1: 依赖检查(@playwright/test + @axe-core/playwright 已安装)
# ════════════════════════════════════════════════════════════════


def check_dependencies(skip_node_require: bool = False) -> CheckResult:
    """验证 @playwright/test 与 @axe-core/playwright 已安装且可 require。

    R62 P1-06: 依赖缺失必须硬失败。旧实现 try/catch require + stub replacement
    导致 0 用例 = 绿色 CI(假绿)。本检查通过 node require 验证依赖真实可加载。

    Args:
        skip_node_require: 跳过 node require 子进程检查(本地无 node 时使用,
            仅检查 node_modules 目录存在性)。CI 环境必须为 False。

    Returns:
        CheckResult: ok=True 表示两个依赖均已安装且可 require
    """
    details: dict = {"playwright": False, "axe": False, "errors": []}

    # (a) 静态检查: node_modules 目录存在性
    pw_pkg = E2E_DIR / "node_modules" / "@playwright" / "test" / "package.json"
    axe_pkg = E2E_DIR / "node_modules" / "@axe-core" / "playwright" / "package.json"

    if not pw_pkg.exists():
        details["errors"].append(
            f"MISSING: @playwright/test not found at {_relpath(pw_pkg)}"
        )
    else:
        details["playwright"] = True

    if not axe_pkg.exists():
        details["errors"].append(
            f"MISSING: @axe-core/playwright not found at {_relpath(axe_pkg)}"
        )
    else:
        details["axe"] = True

    # (b) 动态检查: node require 验证可加载(CI 必须,本地可选跳过)
    if not skip_node_require:
        for pkg_name in ("@playwright/test", "@axe-core/playwright"):
            try:
                result = subprocess.run(
                    ["node", "-e", f"require({pkg_name!r})"],
                    cwd=str(E2E_DIR),
                    capture_output=True,
                    timeout=30,
                    text=True,
                )
                if result.returncode != 0:
                    details["errors"].append(
                        f"require({pkg_name!r}) failed (exit {result.returncode}): "
                        f"{result.stderr.strip()[:200]}"
                    )
                    if pkg_name == "@playwright/test":
                        details["playwright"] = False
                    else:
                        details["axe"] = False
            except FileNotFoundError:
                details["errors"].append(
                    f"node not found on PATH — cannot verify require({pkg_name!r})"
                )
            except subprocess.TimeoutExpired:
                details["errors"].append(
                    f"require({pkg_name!r}) timed out (>30s)"
                )

    if details["errors"]:
        return CheckResult(
            ok=False,
            message=(
                "R62 P1-06: a11y 依赖检查失败 — "
                + "; ".join(details["errors"])
            ),
            details=details,
        )
    return CheckResult(
        ok=True,
        message="R62 P1-06: 依赖检查通过(@playwright/test + @axe-core/playwright 均已安装)",
        details=details,
    )


# ════════════════════════════════════════════════════════════════
# 检查 2: 用例数检查(> 0,检测 stub 替换)
# ════════════════════════════════════════════════════════════════


def _list_playwright_cases() -> tuple[list[str], str]:
    """运行 ``npx playwright test --list`` 列出 spec 文件的用例。

    Returns:
        (用例名列表, stderr_or_error_message)
        用例名列表为空时,stderr 包含失败原因
    """
    if not SPEC_FILE.exists():
        return [], f"spec file not found: {_relpath(SPEC_FILE)}"
    try:
        result = subprocess.run(
            [
                "npx", "playwright", "test", "--list",
                SPEC_FILE.name,
            ],
            cwd=str(E2E_DIR),
            capture_output=True,
            timeout=120,
            text=True,
        )
    except FileNotFoundError:
        return [], "npx not found on PATH — cannot list test cases"
    except subprocess.TimeoutExpired:
        return [], "npx playwright test --list timed out (>120s)"
    if result.returncode != 0:
        return [], (
            f"npx playwright test --list failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:300]}"
        )
    # playwright --list 输出每行一个用例(过滤空行 + 警告行)
    lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.strip().startswith("Warning:")
    ]
    return lines, ""


def check_test_case_count(skip_playwright: bool = False) -> CheckResult:
    """验证 a11y 测试用例数 > 0(检测 stub 替换导致的假绿)。

    R62 P1-06: 旧实现的 stub replacement(``test = { describe: () => {}, ... }``)
    会导致 ``test.describe`` body 不执行 → 0 用例 = 绿色 CI(假绿)。
    本检查通过 ``npx playwright test --list`` 验证用例数 > 0。

    Args:
        skip_playwright: 跳过 npx playwright 子进程检查(本地无 node/npx 时使用)。
            CI 环境必须为 False。

    Returns:
        CheckResult: ok=True 表示用例数 > 0;details["count"] 为用例数
    """
    details: dict = {"count": 0, "sample_cases": []}

    if skip_playwright:
        # 静态检查: spec 文件存在且非空
        if not SPEC_FILE.exists():
            return CheckResult(
                ok=False,
                message=f"R62 P1-06: spec 文件不存在: {SPEC_FILE}",
                details=details,
            )
        content = SPEC_FILE.read_text(encoding="utf-8")
        # 检测 stub replacement(若存在则用例数必然为 0)
        if "test = {" in content and "describe: () => {}" in content:
            return CheckResult(
                ok=False,
                message="R62 P1-06: 检测到 stub replacement(test = { describe: () => {} })— 用例数为 0",
                details=details,
            )
        return CheckResult(
            ok=True,
            message="R62 P1-06: 用例数检查跳过(--skip-playwright),静态检查通过(无 stub replacement)",
            details=details,
        )

    cases, err = _list_playwright_cases()
    details["count"] = len(cases)
    details["sample_cases"] = cases[:5]

    if err:
        return CheckResult(
            ok=False,
            message=f"R62 P1-06: 无法列出测试用例 — {err}",
            details=details,
        )
    if len(cases) == 0:
        return CheckResult(
            ok=False,
            message=(
                "R62 P1-06: 0 a11y test cases found — stub replacement detected "
                "(npx playwright test --list 返回 0 行)"
            ),
            details=details,
        )
    return CheckResult(
        ok=True,
        message=f"R62 P1-06: 用例数检查通过({len(cases)} 个用例)",
        details=details,
    )


# ════════════════════════════════════════════════════════════════
# 检查 3: locale 覆盖检查(zh-CN + en-US 都有对应用例)
# ════════════════════════════════════════════════════════════════


def check_locale_coverage(
    case_names: Optional[list[str]] = None,
    test_output: Optional[str] = None,
) -> CheckResult:
    """验证 zh-CN 与 en-US locale 都有对应的测试用例或测试输出。

    R62 P1-06: /login en-US 旧实现复用 zh-CN 结果(跳过 /locale 切换),
    导致 en-US locale 实际未被执行。本检查验证:

    Args:
        case_names: ``npx playwright test --list`` 输出的用例名列表。
            若提供,检查用例名同时包含 ``zh-CN`` 与 ``en-US`` 标记。
        test_output: 测试运行后的输出文本(可选)。
            若提供,检查输出同时包含 ``zh-CN`` 与 ``en-US`` 标记。

    Returns:
        CheckResult: ok=True 表示两个 locale 都有覆盖
    """
    details: dict = {"zh-CN": False, "en-US": False, "missing": []}

    sources = []
    if case_names is not None:
        sources.append(("case_names", case_names))
    if test_output is not None:
        sources.append(("test_output", test_output.splitlines()))

    if not sources:
        # 兜底:从 _list_playwright_cases 获取
        cases, err = _list_playwright_cases()
        if err:
            return CheckResult(
                ok=False,
                message=f"R62 P1-06: locale 覆盖检查失败 — 无法获取用例列表: {err}",
                details=details,
            )
        sources.append(("case_names", cases))

    # 检查每个 locale 是否在任一来源中出现
    for locale in EXPECTED_LOCALES:
        found = False
        for source_name, lines in sources:
            if any(locale in line for line in lines):
                found = True
                details[locale] = True
                break
        if not found:
            details["missing"].append(locale)

    if details["missing"]:
        return CheckResult(
            ok=False,
            message=(
                "R62 P1-06: locale 覆盖检查失败 — 以下 locale 未被执行: "
                + ", ".join(details["missing"])
                + "(审计要求 zh-CN 与 en-US 都必须有独立测试用例)"
            ),
            details=details,
        )
    return CheckResult(
        ok=True,
        message="R62 P1-06: locale 覆盖检查通过(zh-CN + en-US 均有对应用例)",
        details=details,
    )


# ════════════════════════════════════════════════════════════════
# 检查 4: 路由元数据完整性检查
# ════════════════════════════════════════════════════════════════


def check_route_metadata() -> CheckResult:
    """验证 generated_a11y_cases.json 中每条 a11y_testable 用例的路由元数据完整。

    R62 P1-06: 路由元数据缺失必须硬失败。每条 ``a11y_testable=true`` 的用例
    必须有以下字段: ``path``, ``route_path``, ``method``, ``permission``。

    Returns:
        CheckResult: ok=True 表示所有用例元数据完整
    """
    details: dict = {
        "total_cases": 0,
        "a11y_testable_cases": 0,
        "missing_fields": [],
    }

    if not CASES_JSON.exists():
        return CheckResult(
            ok=False,
            message=f"R62 P1-06: generated_a11y_cases.json 不存在: {_relpath(CASES_JSON)}",
            details=details,
        )

    try:
        cases = json.loads(CASES_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return CheckResult(
            ok=False,
            message=f"R62 P1-06: generated_a11y_cases.json 解析失败: {e}",
            details=details,
        )

    if not isinstance(cases, list):
        return CheckResult(
            ok=False,
            message="R62 P1-06: generated_a11y_cases.json 顶层应为数组",
            details=details,
        )

    details["total_cases"] = len(cases)
    a11y_cases = [
        c for c in cases
        if isinstance(c, dict) and c.get("a11y_testable") is True and c.get("method") == "GET"
    ]
    details["a11y_testable_cases"] = len(a11y_cases)

    if not a11y_cases:
        return CheckResult(
            ok=False,
            message="R62 P1-06: 0 条 a11y_testable 用例(生成器可能未运行或路由元数据全部 a11y_testable=False)",
            details=details,
        )

    required_fields = ("path", "route_path", "method", "permission")
    valid_permissions = ("require_session", "public")

    for caze in a11y_cases:
        for field in required_fields:
            val = caze.get(field)
            if not val or not isinstance(val, str):
                details["missing_fields"].append(
                    f"path={caze.get('path', '?')} missing/invalid '{field}'"
                )
        perm = caze.get("permission")
        if perm not in valid_permissions:
            details["missing_fields"].append(
                f"path={caze.get('path', '?')} invalid 'permission'={perm!r}"
            )

    if details["missing_fields"]:
        return CheckResult(
            ok=False,
            message=(
                f"R62 P1-06: 路由元数据不完整 — {len(details['missing_fields'])} 项缺失/无效: "
                + "; ".join(details["missing_fields"][:5])
            ),
            details=details,
        )
    return CheckResult(
        ok=True,
        message=(
            f"R62 P1-06: 路由元数据检查通过({len(a11y_cases)} 条 a11y_testable 用例,"
            f"字段完整: path / route_path / method / permission)"
        ),
        details=details,
    )


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════


def run_all_checks(
    skip_node_require: bool = False,
    skip_playwright: bool = False,
    test_output_file: Optional[Path] = None,
) -> tuple[list[CheckResult], list[CheckResult]]:
    """运行所有预检查,返回(通过列表, 失败列表)。

    Args:
        skip_node_require: 跳过 node require 子进程检查
        skip_playwright: 跳过 npx playwright 子进程检查
        test_output_file: 测试输出文件路径(用于 locale 执行验证)

    Returns:
        (passed, failed) — 通过与失败的检查结果列表
    """
    results: list[CheckResult] = []

    # 1. 依赖检查
    results.append(check_dependencies(skip_node_require=skip_node_require))

    # 2. 用例数检查
    case_count_result = check_test_case_count(skip_playwright=skip_playwright)
    results.append(case_count_result)

    # 3. locale 覆盖检查
    test_output: Optional[str] = None
    if test_output_file and test_output_file.exists():
        test_output = test_output_file.read_text(encoding="utf-8", errors="replace")

    # 若 playwright 可用,从用例列表获取 locale 覆盖;否则:
    #   - 优先用 test_output(若提供)
    #   - 兜底用 SPEC_FILE 静态内容(检测 spec 文件内是否含 zh-CN / en-US 标记)
    # R62 P1-06: skip_playwright=True 时不能调用 _list_playwright_cases()
    #   (会触发 npx playwright test --list 子进程,在合成/本地环境返回
    #   "No tests found" → 误判 locale 缺失 = 假红)。
    if not skip_playwright and case_count_result.ok:
        cases, _ = _list_playwright_cases()
        results.append(check_locale_coverage(case_names=cases, test_output=test_output))
    else:
        # 静态兜底:从 SPEC_FILE 内容查找 locale 标记(zh-CN / en-US 字面量)
        # 与 check_test_case_count 的 skip_playwright 静态检查一致
        spec_lines: list[str] = []
        if SPEC_FILE.exists():
            spec_lines = SPEC_FILE.read_text(encoding="utf-8").splitlines()
        results.append(
            check_locale_coverage(
                case_names=spec_lines if spec_lines else None,
                test_output=test_output,
            )
        )

    # 4. 路由元数据检查
    results.append(check_route_metadata())

    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    return passed, failed


def main() -> int:
    """CLI 主入口。返回 0(全部通过)或 1(任一失败)。"""
    parser = argparse.ArgumentParser(
        description="R62 P1-06: a11y 测试预检查 — 检测依赖缺失/0 用例/locale 缺失/元数据不全"
    )
    parser.add_argument(
        "--skip-node-require",
        action="store_true",
        help="跳过 node require 子进程检查(本地无 node 时使用)",
    )
    parser.add_argument(
        "--skip-playwright",
        action="store_true",
        help="跳过 npx playwright 子进程检查(本地无 npx 时使用)",
    )
    parser.add_argument(
        "--test-output",
        type=Path,
        default=None,
        help="测试输出文件路径(用于 locale 执行验证,检查输出中的 locale 标记)",
    )
    args = parser.parse_args()

    passed, failed = run_all_checks(
        skip_node_require=args.skip_node_require,
        skip_playwright=args.skip_playwright,
        test_output_file=args.test_output,
    )

    # 输出结果(通过的用 stdout,失败的用 stderr)
    for r in passed:
        print(f"[PASS] {r.message}")
    for r in failed:
        print(f"[FAIL] {r.message}", file=sys.stderr)
        if r.details:
            for k, v in r.details.items():
                if isinstance(v, list) and v:
                    print(f"       {k}: {v[:3]}", file=sys.stderr)
                elif v:
                    print(f"       {k}: {v}", file=sys.stderr)

    if failed:
        print(
            f"\n[R62 P1-06] 预检查失败: {len(failed)}/{len(passed) + len(failed)} 项未通过",
            file=sys.stderr,
        )
        return 1
    print(f"\n[R62 P1-06] 所有预检查通过({len(passed)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
