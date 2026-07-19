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
# R64 P1-09: a11y 强制矩阵用例文件(独立于 R61 路由用例文件)
# - generated_a11y_cases.json: R61 路由级用例(每条 GET 路由一个 case,list schema)
# - generated_a11y_matrix_cases.json: R64 矩阵用例(2 locales × 2 input_modes × 16 states = 64 case, dict schema)
# 两个文件服务于不同测试维度,共存不冲突(路由覆盖 + 状态覆盖)。
MATRIX_CASES_JSON = E2E_DIR / "generated_a11y_matrix_cases.json"

# R62 P1-06: 预期的 locale 标记(测试用例名应同时包含两者)
EXPECTED_LOCALES = ("zh-CN", "en-US")

# R64 P1-09: a11y 强制矩阵维度
# - locales: zh-CN / en-US(2 个)
# - input_modes: keyboard / screen_reader(2 个)
# - states: 16 个状态覆盖所有 UI 形态(错误/加载/空/分页/模态框/动态按钮/
#   权限不足/审批与 MFA 全状态)
EXPECTED_INPUT_MODES: tuple[str, ...] = ("keyboard", "screen_reader")
EXPECTED_STATES: tuple[str, ...] = (
    "error", "loading", "empty", "paginated", "modal", "dynamic_button",
    "permission_denied",
    "approval_required", "approval_pending", "approval_approved", "approval_rejected",
    "mfa_required", "mfa_pending", "mfa_verified",
    # 兼容性别名(允许 generated_a11y_cases.json 用以下别名替代部分状态)
    "mfa_expired", "approval_expired",
)
# 完整 16 状态集合(用于门禁脚本声明完整矩阵)
EXPECTED_STATES_FULL_COUNT: int = 16
# 矩阵规模: 2 locales × 2 input_modes × 16 states = 64 个用例
EXPECTED_MATRIX_CASE_COUNT: int = len(EXPECTED_LOCALES) * len(EXPECTED_INPUT_MODES) * EXPECTED_STATES_FULL_COUNT


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
# 检查 5: a11y 矩阵完整性检查(R64 P1-09 强制矩阵)
# ════════════════════════════════════════════════════════════════


def check_matrix_completeness(cases_json: Optional[Path] = None) -> CheckResult:
    """R64 P1-09: 验证 a11y 矩阵用例文件覆盖完整矩阵。

    强制规则:
        1. expected_count 字段必须存在且为正整数
        2. expected_count == EXPECTED_MATRIX_CASE_COUNT(2 × 2 × 16 = 64)
        3. 实际用例数 == expected_count
        4. 矩阵必须覆盖 EXPECTED_LOCALES × EXPECTED_INPUT_MODES ×
           16 个 state(error/loading/empty/paginated/modal/dynamic_button/
           permission_denied/approval_*/mfa_*)

    缺失任何维度 → fail-closed(ok=False)。

    Args:
        cases_json: 矩阵用例文件路径(默认 MATRIX_CASES_JSON;
            测试可传入临时路径)。

    Returns:
        CheckResult: ok=True 表示矩阵完整;details 含
        {expected_count, actual_count, missing_locales,
         missing_input_modes, missing_states}
    """
    details: dict = {
        "expected_count": 0,
        "actual_count": 0,
        "missing_locales": [],
        "missing_input_modes": [],
        "missing_states": [],
        "matrix_locales": [],
        "matrix_input_modes": [],
        "matrix_states": [],
    }

    json_path = cases_json or MATRIX_CASES_JSON
    if not json_path.exists():
        return CheckResult(
            ok=False,
            message=f"R64 P1-09: 矩阵用例文件不存在: {_relpath(json_path)}",
            details=details,
        )

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return CheckResult(
            ok=False,
            message=f"R64 P1-09: 矩阵用例文件解析失败: {e}",
            details=details,
        )

    # 兼容两种 schema:
    #   (a) 顶层为数组(原始 R61 schema,无 expected_count)
    #   (b) 顶层为对象 {expected_count, cases: [...]}(R64 P1-09 schema)
    if isinstance(data, list):
        cases = data
        expected_count = 0  # 旧 schema 无 expected_count
    elif isinstance(data, dict):
        cases = data.get("cases", [])
        if not isinstance(cases, list):
            return CheckResult(
                ok=False,
                message="R64 P1-09: generated_a11y_cases.json 顶层对象的 cases 字段应为数组",
                details=details,
            )
        expected_count = int(data.get("expected_count") or 0)
    else:
        return CheckResult(
            ok=False,
            message="R64 P1-09: generated_a11y_cases.json 顶层应为数组或对象",
            details=details,
        )

    details["expected_count"] = expected_count
    details["actual_count"] = len(cases)

    # 收集 cases 中出现的 locales / input_modes / states
    seen_locales: set[str] = set()
    seen_input_modes: set[str] = set()
    seen_states: set[str] = set()
    for caze in cases:
        if not isinstance(caze, dict):
            continue
        # locale 字段(可能为 "locale" 或 "locales" 数组)
        loc = caze.get("locale")
        if isinstance(loc, str) and loc:
            seen_locales.add(loc)
        locs = caze.get("locales")
        if isinstance(locs, list):
            for l in locs:
                if isinstance(l, str) and l:
                    seen_locales.add(l)
        # input_mode 字段(可能为 "input_mode" 或 "input_modes" 数组)
        im = caze.get("input_mode")
        if isinstance(im, str) and im:
            seen_input_modes.add(im)
        ims = caze.get("input_modes")
        if isinstance(ims, list):
            for m in ims:
                if isinstance(m, str) and m:
                    seen_input_modes.add(m)
        # state 字段(可能为 "state" 或 "states" 数组)
        st = caze.get("state")
        if isinstance(st, str) and st:
            seen_states.add(st)
        sts = caze.get("states")
        if isinstance(sts, list):
            for s in sts:
                if isinstance(s, str) and s:
                    seen_states.add(s)

    details["matrix_locales"] = sorted(seen_locales)
    details["matrix_input_modes"] = sorted(seen_input_modes)
    details["matrix_states"] = sorted(seen_states)

    # 检查 expected_count 完整性
    if expected_count <= 0:
        details["missing_locales"].extend(EXPECTED_LOCALES)
        details["missing_input_modes"].extend(EXPECTED_INPUT_MODES)
        details["missing_states"].extend(EXPECTED_STATES)
        return CheckResult(
            ok=False,
            message=(
                "R64 P1-09: generated_a11y_cases.json 缺少 expected_count 字段"
                "(R64 P1-09 强制矩阵要求 expected_count == 实际用例数)"
            ),
            details=details,
        )

    if expected_count != EXPECTED_MATRIX_CASE_COUNT:
        return CheckResult(
            ok=False,
            message=(
                f"R64 P1-09: expected_count={expected_count} 与"
                f" EXPECTED_MATRIX_CASE_COUNT={EXPECTED_MATRIX_CASE_COUNT}"
                f"(2 locales × 2 input_modes × 16 states)不一致"
            ),
            details=details,
        )

    # 检查实际用例数 == expected_count
    if len(cases) != expected_count:
        return CheckResult(
            ok=False,
            message=(
                f"R64 P1-09: 实际用例数 {len(cases)} != expected_count {expected_count}"
                f"(expected_count 必须与实际 executed 完全相等,任何偏差视为假绿)"
            ),
            details=details,
        )

    # 检查矩阵覆盖完整性(locale × input_mode × state)
    missing_locales = [l for l in EXPECTED_LOCALES if l not in seen_locales]
    missing_input_modes = [m for m in EXPECTED_INPUT_MODES if m not in seen_input_modes]
    missing_states = [s for s in EXPECTED_STATES if s not in seen_states]
    details["missing_locales"] = missing_locales
    details["missing_input_modes"] = missing_input_modes
    details["missing_states"] = missing_states

    if missing_locales or missing_input_modes or missing_states:
        missing_summary: list[str] = []
        if missing_locales:
            missing_summary.append(f"locales={missing_locales}")
        if missing_input_modes:
            missing_summary.append(f"input_modes={missing_input_modes}")
        if missing_states:
            missing_summary.append(f"states={missing_states}")
        return CheckResult(
            ok=False,
            message=(
                "R64 P1-09: a11y 矩阵缺失维度 — "
                + "; ".join(missing_summary)
                + "(强制矩阵要求覆盖 2 locales × 2 input_modes × 16 states)"
            ),
            details=details,
        )

    return CheckResult(
        ok=True,
        message=(
            f"R64 P1-09: a11y 矩阵完整性检查通过"
            f"(expected_count={expected_count}, 实际用例数={len(cases)},"
            f"locales={sorted(seen_locales)}, input_modes={sorted(seen_input_modes)},"
            f"states={len(seen_states)} 个)"
        ),
        details=details,
    )


def check_expected_count_equals_executed(
    executed_count: int,
    *,
    skip_count: int = 0,
    xpass_count: int = 0,
) -> CheckResult:
    """R64 P1-09: 验证 expected_count == executed_count,且无 skip / xpass。

    审计 P1-09 强制规则:
        1. expected_test_count 必须等于 executed_test_count
        2. 任何 skip / xpass 视为失败(exit code != 0)

    Args:
        executed_count: 实际执行的测试用例数
        skip_count: skip 数量(必须为 0)
        xpass_count: xpass 数量(必须为 0)

    Returns:
        CheckResult: ok=True 表示 expected == executed 且 skip/xpass 均为 0
    """
    details: dict = {
        "expected_count": EXPECTED_MATRIX_CASE_COUNT,
        "executed_count": executed_count,
        "skip_count": skip_count,
        "xpass_count": xpass_count,
    }
    if executed_count != EXPECTED_MATRIX_CASE_COUNT:
        return CheckResult(
            ok=False,
            message=(
                f"R64 P1-09: expected_count({EXPECTED_MATRIX_CASE_COUNT}) != "
                f"executed_count({executed_count})"
            ),
            details=details,
        )
    if skip_count > 0:
        return CheckResult(
            ok=False,
            message=(
                f"R64 P1-09: 检测到 {skip_count} 个 skip 用例"
                f"(任何 skip 视为失败,不允许跳过矩阵用例)"
            ),
            details=details,
        )
    if xpass_count > 0:
        return CheckResult(
            ok=False,
            message=(
                f"R64 P1-09: 检测到 {xpass_count} 个 xpass 用例"
                f"(任何 xpass 视为失败,不允许 xfail 标记残留)"
            ),
            details=details,
        )
    return CheckResult(
        ok=True,
        message=(
            f"R64 P1-09: expected == executed({executed_count}),"
            f"skip=0, xpass=0"
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
    *,
    enforce_matrix: bool = False,
) -> tuple[list[CheckResult], list[CheckResult]]:
    """运行所有预检查,返回(通过列表, 失败列表)。

    Args:
        skip_node_require: 跳过 node require 子进程检查
        skip_playwright: 跳过 npx playwright 子进程检查
        test_output_file: 测试输出文件路径(用于 locale 执行验证)
        enforce_matrix: R64 P1-09 强制矩阵检查开关。
            True: 额外运行 check_matrix_completeness(),
                校验 generated_a11y_cases.json 是否覆盖
                2 locales × 2 input_modes × 16 states = 64 用例。
            False(默认): 不运行矩阵检查,保持 R62 P1-06
                向后兼容(legacy list schema 无 expected_count)。
            CI Scanner 9 通过 check_a11y_matrix_enforcement.py 单独调用。

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

    # 5. R64 P1-09: a11y 矩阵完整性检查(opt-in,默认关闭以保持向后兼容)
    #    CI Scanner 9 单独调用 check_a11y_matrix_enforcement.py 强制矩阵。
    if enforce_matrix:
        results.append(check_matrix_completeness())

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
    parser.add_argument(
        "--enforce-matrix",
        action="store_true",
        help="R64 P1-09: 启用 a11y 矩阵完整性检查"
        "(校验 generated_a11y_cases.json 覆盖 2 locales × 2 input_modes × 16 states)",
    )
    args = parser.parse_args()

    passed, failed = run_all_checks(
        skip_node_require=args.skip_node_require,
        skip_playwright=args.skip_playwright,
        test_output_file=args.test_output,
        enforce_matrix=args.enforce_matrix,
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
