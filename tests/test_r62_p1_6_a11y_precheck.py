"""R62 P1-06: a11y 测试预检查脚本的单测。

审计 P1-06: 测试文件注释明确"运行时若 Playwright 或 axe 依赖缺失,test.describe 自动 skip"。
对 release gate 来说,这是典型假绿条件。

整改: ``scripts/check_a11y_precheck.py`` 在 E2E 测试运行前预检查:
    1. 依赖检查: ``@playwright/test`` + ``@axe-core/playwright`` 已安装
    2. 用例数检查: ``accessibility_behavior.spec.ts`` 用例数 > 0(检测 stub 替换)
    3. locale 覆盖检查: zh-CN + en-US 都有对应用例
    4. 路由元数据检查: 每条 a11y_testable 用例字段完整

本测试覆盖:
    A. ``check_dependencies`` — 依赖缺失检测
    B. ``check_test_case_count`` — 0 用例 / stub 替换检测
    C. ``check_locale_coverage`` — locale 缺失检测
    D. ``check_route_metadata`` — 路由元数据不完整检测
    E. ``run_all_checks`` — 聚合检查的失败传递
    F. CLI ``main()`` — 退出码正确性
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 让测试能导入 scripts/check_a11y_precheck.py
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_a11y_precheck as precheck  # type: ignore  # noqa: E402


# ════════════════════════════════════════════════════════════════
# A. check_dependencies — 依赖缺失检测
# ════════════════════════════════════════════════════════════════


class TestCheckDependencies:
    """A 节: 依赖检查正确检测缺失的 @playwright/test 与 @axe-core/playwright。"""

    def test_detects_missing_playwright_package(self, tmp_path: Path, monkeypatch):
        """A1: @playwright/test 缺失时,check_dependencies 必须返回 ok=False。"""
        # 构造一个空的 e2e 目录(无 node_modules)
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        # 创建 axe 的假目录(只缺 playwright)
        axe_pkg = fake_e2e / "node_modules" / "@axe-core" / "playwright" / "package.json"
        axe_pkg.parent.mkdir(parents=True)
        axe_pkg.write_text('{"name": "@axe-core/playwright"}')

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_dependencies(skip_node_require=True)

        assert result.ok is False
        assert "@playwright/test" in result.message or "playwright" in result.message.lower()
        assert result.details["playwright"] is False
        assert result.details["axe"] is True  # axe 存在

    def test_detects_missing_axe_package(self, tmp_path: Path, monkeypatch):
        """A2: @axe-core/playwright 缺失时,check_dependencies 必须返回 ok=False。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        # 创建 playwright 的假目录(只缺 axe)
        pw_pkg = fake_e2e / "node_modules" / "@playwright" / "test" / "package.json"
        pw_pkg.parent.mkdir(parents=True)
        pw_pkg.write_text('{"name": "@playwright/test"}')

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_dependencies(skip_node_require=True)

        assert result.ok is False
        assert "@axe-core/playwright" in result.message or "axe" in result.message.lower()
        assert result.details["playwright"] is True
        assert result.details["axe"] is False

    def test_passes_when_both_packages_installed(self, tmp_path: Path, monkeypatch):
        """A3: 两个依赖均安装时,check_dependencies 必须返回 ok=True。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        for pkg_path, pkg_name in [
            ("node_modules/@playwright/test/package.json", "@playwright/test"),
            ("node_modules/@axe-core/playwright/package.json", "@axe-core/playwright"),
        ]:
            full = fake_e2e / pkg_path
            full.parent.mkdir(parents=True)
            full.write_text(f'{{"name": "{pkg_name}"}}')

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_dependencies(skip_node_require=True)

        assert result.ok is True
        assert result.details["playwright"] is True
        assert result.details["axe"] is True
        assert not result.details["errors"]

    def test_detects_both_missing(self, tmp_path: Path, monkeypatch):
        """A4: 两个依赖均缺失时,check_dependencies 必须返回 ok=False 并报告两项缺失。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_dependencies(skip_node_require=True)

        assert result.ok is False
        assert len(result.details["errors"]) == 2
        assert result.details["playwright"] is False
        assert result.details["axe"] is False


# ════════════════════════════════════════════════════════════════
# B. check_test_case_count — 0 用例 / stub 替换检测
# ════════════════════════════════════════════════════════════════


class TestCheckTestCaseCount:
    """B 节: 用例数检查正确检测 0 用例与 stub 替换。"""

    def test_detects_zero_cases(self, tmp_path: Path, monkeypatch):
        """B1: ``npx playwright test --list`` 返回 0 行时,必须返回 ok=False。"""
        # mock _list_playwright_cases 返回空列表
        monkeypatch.setattr(precheck, "_list_playwright_cases", lambda: ([], ""))
        monkeypatch.setattr(precheck, "SPEC_FILE", tmp_path / "fake.spec.ts")

        result = precheck.check_test_case_count(skip_playwright=False)

        assert result.ok is False
        assert result.details["count"] == 0
        assert "0" in result.message or "stub" in result.message.lower()

    def test_detects_list_error(self, tmp_path: Path, monkeypatch):
        """B2: ``npx playwright test --list`` 报错时,必须返回 ok=False 并传递错误信息。"""
        err_msg = "npx not found on PATH"
        monkeypatch.setattr(precheck, "_list_playwright_cases", lambda: ([], err_msg))
        monkeypatch.setattr(precheck, "SPEC_FILE", tmp_path / "fake.spec.ts")

        result = precheck.check_test_case_count(skip_playwright=False)

        assert result.ok is False
        assert err_msg in result.message

    def test_passes_with_nonzero_cases(self, tmp_path: Path, monkeypatch):
        """B3: 用例数 > 0 时,必须返回 ok=True 并记录用例数。"""
        fake_cases = [
            "  ✓  root (zh-CN): axe + 键盘 + 焦点",
            "  ✓  root (en-US): axe + 键盘 + 焦点",
            "  ✓  login (zh-CN): axe + 键盘",
            "  ✓  login (en-US): Accept-Language + locale cookie",
        ]
        monkeypatch.setattr(precheck, "_list_playwright_cases", lambda: (fake_cases, ""))
        monkeypatch.setattr(precheck, "SPEC_FILE", tmp_path / "fake.spec.ts")

        result = precheck.check_test_case_count(skip_playwright=False)

        assert result.ok is True
        assert result.details["count"] == 4
        assert result.details["sample_cases"] == fake_cases[:5]

    def test_detects_stub_replacement_static(self, tmp_path: Path, monkeypatch):
        """B4: 静态检查模式(--skip-playwright)检测 spec 文件中的 stub replacement。"""
        fake_spec = tmp_path / "accessibility_behavior.spec.ts"
        # 写入包含 stub replacement 的内容(旧实现的假绿模式)
        fake_spec.write_text(
            "const SKIP_REASON = '!playwrightAvailable';\n"
            "if (SKIP_REASON) {\n"
            "  test = {\n"
            "    describe: () => {},\n"
            "    test: () => {},\n"
            "  };\n"
            "}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(precheck, "SPEC_FILE", fake_spec)

        result = precheck.check_test_case_count(skip_playwright=True)

        assert result.ok is False
        assert "stub replacement" in result.message.lower()

    def test_passes_static_check_without_stub(self, tmp_path: Path, monkeypatch):
        """B5: 静态检查模式(--skip-playwright)对无 stub 的 spec 文件返回 ok=True。"""
        fake_spec = tmp_path / "accessibility_behavior.spec.ts"
        # 写入正常的 spec 内容(无 stub replacement)
        fake_spec.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test.describe('a11y', () => { test('case', () => {}); });\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(precheck, "SPEC_FILE", fake_spec)

        result = precheck.check_test_case_count(skip_playwright=True)

        assert result.ok is True

    def test_detects_missing_spec_file_static(self, tmp_path: Path, monkeypatch):
        """B6: 静态检查模式检测 spec 文件不存在。"""
        monkeypatch.setattr(precheck, "SPEC_FILE", tmp_path / "nonexistent.spec.ts")

        result = precheck.check_test_case_count(skip_playwright=True)

        assert result.ok is False
        assert "不存在" in result.message or "not found" in result.message.lower()


# ════════════════════════════════════════════════════════════════
# C. check_locale_coverage — locale 缺失检测
# ════════════════════════════════════════════════════════════════


class TestCheckLocaleCoverage:
    """C 节: locale 覆盖检查正确检测 zh-CN / en-US 缺失。"""

    def test_detects_missing_en_us_locale(self):
        """C1: 用例列表仅有 zh-CN 时,必须返回 ok=False(检测 en-US 复用 zh-CN 的假绿)。"""
        case_names = [
            "  ✓  root (zh-CN): axe + 键盘",
            "  ✓  login (zh-CN): axe + 键盘",
        ]

        result = precheck.check_locale_coverage(case_names=case_names)

        assert result.ok is False
        assert "en-US" in result.details["missing"]
        assert result.details["zh-CN"] is True
        assert result.details["en-US"] is False

    def test_detects_missing_zh_cn_locale(self):
        """C2: 用例列表仅有 en-US 时,必须返回 ok=False。"""
        case_names = [
            "  ✓  root (en-US): axe + 键盘",
            "  ✓  login (en-US): Accept-Language",
        ]

        result = precheck.check_locale_coverage(case_names=case_names)

        assert result.ok is False
        assert "zh-CN" in result.details["missing"]
        assert result.details["zh-CN"] is False
        assert result.details["en-US"] is True

    def test_passes_with_both_locales(self):
        """C3: 用例列表同时包含 zh-CN 与 en-US 时,必须返回 ok=True。"""
        case_names = [
            "  ✓  root (zh-CN): axe + 键盘",
            "  ✓  root (en-US): axe + 键盘",
            "  ✓  login (zh-CN): axe + 键盘",
            "  ✓  login (en-US): Accept-Language + locale cookie",
        ]

        result = precheck.check_locale_coverage(case_names=case_names)

        assert result.ok is True
        assert result.details["zh-CN"] is True
        assert result.details["en-US"] is True
        assert not result.details["missing"]

    def test_detects_both_locales_missing(self):
        """C4: 用例列表无 locale 标记时,必须返回 ok=False 并报告两个 locale 都缺失。"""
        case_names = [
            "  ✓  some test without locale marker",
            "  ✓  another test",
        ]

        result = precheck.check_locale_coverage(case_names=case_names)

        assert result.ok is False
        assert len(result.details["missing"]) == 2
        assert "zh-CN" in result.details["missing"]
        assert "en-US" in result.details["missing"]

    def test_detects_missing_locale_in_test_output(self):
        """C5: 测试输出缺少 en-US 标记时,必须返回 ok=False(实际执行验证)。"""
        # 测试输出只有 zh-CN(模拟 /login en-US 复用 zh-CN 的假绿)
        test_output = (
            "[R62 P1-06] running root (zh-CN)...\n"
            "  ✓  root (zh-CN) passed\n"
            "[R62 P1-06] running login (zh-CN)...\n"
            "  ✓  login (zh-CN) passed\n"
            # 没有 en-US 标记
        )

        result = precheck.check_locale_coverage(test_output=test_output)

        assert result.ok is False
        assert "en-US" in result.details["missing"]
        assert result.details["zh-CN"] is True
        assert result.details["en-US"] is False

    def test_passes_with_both_locales_in_test_output(self):
        """C6: 测试输出同时包含 zh-CN 与 en-US 标记时,必须返回 ok=True。"""
        test_output = (
            "[R62 P1-06] running root (zh-CN)...\n"
            "  ✓  root (zh-CN) passed\n"
            "[R62 P1-06] running root (en-US)...\n"
            "  ✓  root (en-US) passed\n"
        )

        result = precheck.check_locale_coverage(test_output=test_output)

        assert result.ok is True

    def test_falls_back_to_listing_when_no_input(self, monkeypatch):
        """C7: 未提供 case_names/test_output 时,自动调用 _list_playwright_cases 兜底。"""
        fake_cases = ["  ✓  root (zh-CN)", "  ✓  root (en-US)"]
        monkeypatch.setattr(precheck, "_list_playwright_cases", lambda: (fake_cases, ""))

        result = precheck.check_locale_coverage()

        assert result.ok is True

    def test_fails_when_listing_fails_and_no_input(self, monkeypatch):
        """C8: 未提供输入且 _list_playwright_cases 失败时,必须返回 ok=False。"""
        err = "npx not found"
        monkeypatch.setattr(precheck, "_list_playwright_cases", lambda: ([], err))

        result = precheck.check_locale_coverage()

        assert result.ok is False
        assert err in result.message


# ════════════════════════════════════════════════════════════════
# D. check_route_metadata — 路由元数据完整性检查
# ════════════════════════════════════════════════════════════════


class TestCheckRouteMetadata:
    """D 节: 路由元数据检查正确检测缺失字段。"""

    def test_detects_missing_json_file(self, tmp_path: Path, monkeypatch):
        """D1: generated_a11y_cases.json 不存在时,必须返回 ok=False。"""
        monkeypatch.setattr(precheck, "CASES_JSON", tmp_path / "nonexistent.json")
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert "不存在" in result.message or "not exist" in result.message.lower()

    def test_detects_invalid_json(self, tmp_path: Path, monkeypatch):
        """D2: JSON 解析失败时,必须返回 ok=False。"""
        bad_json = tmp_path / "cases.json"
        bad_json.write_text("{invalid json", encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", bad_json)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert "解析失败" in result.message or "parse" in result.message.lower()

    def test_detects_zero_a11y_testable_cases(self, tmp_path: Path, monkeypatch):
        """D3: 0 条 a11y_testable 用例时,必须返回 ok=False。"""
        cases = [
            {"path": "/api", "method": "GET", "a11y_testable": False},
        ]
        json_path = tmp_path / "cases.json"
        json_path.write_text(json.dumps(cases), encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", json_path)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert "0" in result.message

    def test_detects_missing_path_field(self, tmp_path: Path, monkeypatch):
        """D4: a11y_testable 用例缺少 path 字段时,必须返回 ok=False。"""
        cases = [
            {
                "route_path": "/users",
                "method": "GET",
                "permission": "require_session",
                "a11y_testable": True,
                # 缺少 path
            },
        ]
        json_path = tmp_path / "cases.json"
        json_path.write_text(json.dumps(cases), encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", json_path)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert any("path" in m for m in result.details["missing_fields"])

    def test_detects_missing_route_path_field(self, tmp_path: Path, monkeypatch):
        """D5: a11y_testable 用例缺少 route_path 字段时,必须返回 ok=False。"""
        cases = [
            {
                "path": "/users",
                "method": "GET",
                "permission": "require_session",
                "a11y_testable": True,
                # 缺少 route_path
            },
        ]
        json_path = tmp_path / "cases.json"
        json_path.write_text(json.dumps(cases), encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", json_path)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert any("route_path" in m for m in result.details["missing_fields"])

    def test_detects_invalid_permission(self, tmp_path: Path, monkeypatch):
        """D6: permission 字段值非法时,必须返回 ok=False。"""
        cases = [
            {
                "path": "/users",
                "route_path": "/users",
                "method": "GET",
                "permission": "invalid_permission",  # 非法值
                "a11y_testable": True,
            },
        ]
        json_path = tmp_path / "cases.json"
        json_path.write_text(json.dumps(cases), encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", json_path)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert any("permission" in m for m in result.details["missing_fields"])

    def test_passes_with_complete_metadata(self, tmp_path: Path, monkeypatch):
        """D7: 所有 a11y_testable 用例字段完整时,必须返回 ok=True。"""
        cases = [
            {
                "path": "/users",
                "route_path": "/users",
                "method": "GET",
                "permission": "require_session",
                "a11y_testable": True,
                "template": "users.html",
            },
            {
                "path": "/login",
                "route_path": "/login",
                "method": "GET",
                "permission": "public",
                "a11y_testable": True,
                "template": None,
            },
            # 非 a11y_testable 用例不检查
            {
                "path": "/api",
                "route_path": "/api",
                "method": "GET",
                "permission": "public",
                "a11y_testable": False,
            },
        ]
        json_path = tmp_path / "cases.json"
        json_path.write_text(json.dumps(cases), encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", json_path)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is True
        assert result.details["a11y_testable_cases"] == 2
        assert not result.details["missing_fields"]

    def test_detects_non_array_json(self, tmp_path: Path, monkeypatch):
        """D8: JSON 顶层非数组时,必须返回 ok=False。"""
        json_path = tmp_path / "cases.json"
        json_path.write_text('{"not": "an array"}', encoding="utf-8")
        monkeypatch.setattr(precheck, "CASES_JSON", json_path)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        result = precheck.check_route_metadata()

        assert result.ok is False
        assert "数组" in result.message or "array" in result.message.lower()


# ════════════════════════════════════════════════════════════════
# E. run_all_checks — 聚合检查
# ════════════════════════════════════════════════════════════════


class TestRunAllChecks:
    """E 节: run_all_checks 正确聚合所有检查结果。"""

    def test_returns_failures_when_dependencies_missing(
        self, tmp_path: Path, monkeypatch
    ):
        """E1: 依赖缺失时,run_all_checks 必须返回非空 failures 列表。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        passed, failed = precheck.run_all_checks(
            skip_node_require=True,
            skip_playwright=True,
        )

        # 依赖检查应失败(node_modules 不存在)
        assert len(failed) >= 1
        dep_failures = [f for f in failed if "依赖" in f.message or "depend" in f.message.lower()]
        assert len(dep_failures) >= 1

    def test_returns_passes_when_all_ok(self, tmp_path: Path, monkeypatch):
        """E2: 所有检查通过时,run_all_checks 必须返回空 failures 列表。"""
        # 构造完整的 e2e 目录结构
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        for pkg_path, pkg_name in [
            ("node_modules/@playwright/test/package.json", "@playwright/test"),
            ("node_modules/@axe-core/playwright/package.json", "@axe-core/playwright"),
        ]:
            full = fake_e2e / pkg_path
            full.parent.mkdir(parents=True)
            full.write_text(f'{{"name": "{pkg_name}"}}')

        # 完整的 cases.json
        cases = [
            {
                "path": "/users",
                "route_path": "/users",
                "method": "GET",
                "permission": "require_session",
                "a11y_testable": True,
            },
        ]
        cases_json = fake_e2e / "generated_a11y_cases.json"
        cases_json.write_text(json.dumps(cases), encoding="utf-8")

        # spec 文件(无 stub + 含 zh-CN / en-US locale 标记,供静态 locale 检查)
        spec = fake_e2e / "accessibility_behavior.spec.ts"
        spec.write_text(
            "import { test } from '@playwright/test';\n"
            "// test case: /login (zh-CN)\n"
            "// test case: /login (en-US)\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(precheck, "CASES_JSON", cases_json)
        monkeypatch.setattr(precheck, "SPEC_FILE", spec)

        passed, failed = precheck.run_all_checks(
            skip_node_require=True,
            skip_playwright=True,
        )

        assert len(failed) == 0
        assert len(passed) >= 4  # 至少 4 项检查全部通过

    def test_locale_coverage_uses_test_output_when_provided(
        self, tmp_path: Path, monkeypatch, tmp_path_factory
    ):
        """E3: 提供 test_output_file 时,locale 覆盖检查使用文件内容。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        for pkg_path, pkg_name in [
            ("node_modules/@playwright/test/package.json", "@playwright/test"),
            ("node_modules/@axe-core/playwright/package.json", "@axe-core/playwright"),
        ]:
            full = fake_e2e / pkg_path
            full.parent.mkdir(parents=True)
            full.write_text(f'{{"name": "{pkg_name}"}}')

        cases = [
            {
                "path": "/users",
                "route_path": "/users",
                "method": "GET",
                "permission": "require_session",
                "a11y_testable": True,
            },
        ]
        cases_json = fake_e2e / "generated_a11y_cases.json"
        cases_json.write_text(json.dumps(cases), encoding="utf-8")

        spec = fake_e2e / "accessibility_behavior.spec.ts"
        spec.write_text("import { test } from '@playwright/test';\n", encoding="utf-8")

        # 测试输出同时包含 zh-CN 和 en-US
        test_output = tmp_path / "test_output.log"
        test_output.write_text(
            "running root (zh-CN)...\nrunning root (en-US)...\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(precheck, "CASES_JSON", cases_json)
        monkeypatch.setattr(precheck, "SPEC_FILE", spec)

        passed, failed = precheck.run_all_checks(
            skip_node_require=True,
            skip_playwright=True,
            test_output_file=test_output,
        )

        assert len(failed) == 0


# ════════════════════════════════════════════════════════════════
# F. CLI main() — 退出码正确性
# ════════════════════════════════════════════════════════════════


class TestCliMain:
    """F 节: CLI main() 在检查失败时返回退出码 1,成功时返回 0。"""

    def test_main_returns_1_on_failure(self, tmp_path: Path, monkeypatch, capsys):
        """F1: 任一检查失败时,main() 必须返回 1。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)

        # 模拟命令行参数
        monkeypatch.setattr(
            sys, "argv",
            ["check_a11y_precheck.py", "--skip-node-require", "--skip-playwright"],
        )

        exit_code = precheck.main()

        assert exit_code == 1
        # stderr 应包含失败信息
        captured = capsys.readouterr()
        assert "FAIL" in captured.err or "失败" in captured.err

    def test_main_returns_0_on_success(self, tmp_path: Path, monkeypatch, capsys):
        """F2: 所有检查通过时,main() 必须返回 0。"""
        fake_e2e = tmp_path / "e2e"
        fake_e2e.mkdir()
        for pkg_path, pkg_name in [
            ("node_modules/@playwright/test/package.json", "@playwright/test"),
            ("node_modules/@axe-core/playwright/package.json", "@axe-core/playwright"),
        ]:
            full = fake_e2e / pkg_path
            full.parent.mkdir(parents=True)
            full.write_text(f'{{"name": "{pkg_name}"}}')

        cases = [
            {
                "path": "/users",
                "route_path": "/users",
                "method": "GET",
                "permission": "require_session",
                "a11y_testable": True,
            },
        ]
        cases_json = fake_e2e / "generated_a11y_cases.json"
        cases_json.write_text(json.dumps(cases), encoding="utf-8")

        # spec 文件(无 stub + 含 zh-CN / en-US locale 标记,供静态 locale 检查)
        spec = fake_e2e / "accessibility_behavior.spec.ts"
        spec.write_text(
            "import { test } from '@playwright/test';\n"
            "// test case: /login (zh-CN)\n"
            "// test case: /login (en-US)\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(precheck, "E2E_DIR", fake_e2e)
        monkeypatch.setattr(precheck, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(precheck, "CASES_JSON", cases_json)
        monkeypatch.setattr(precheck, "SPEC_FILE", spec)

        monkeypatch.setattr(
            sys, "argv",
            ["check_a11y_precheck.py", "--skip-node-require", "--skip-playwright"],
        )

        exit_code = precheck.main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "PASS" in captured.out or "通过" in captured.out


# ════════════════════════════════════════════════════════════════
# G. 真实仓库集成测试(使用实际 generated_a11y_cases.json)
# ════════════════════════════════════════════════════════════════


class TestRealRepoIntegration:
    """G 节: 对真实仓库的 generated_a11y_cases.json 执行集成检查。"""

    def test_real_cases_json_has_complete_metadata(self):
        """G1: 真实 generated_a11y_cases.json 的路由元数据必须完整。"""
        # 直接调用 check_route_metadata(使用模块级真实路径)
        result = precheck.check_route_metadata()

        assert result.ok is True, (
            f"真实 generated_a11y_cases.json 元数据不完整: {result.message}\n"
            f"missing_fields: {result.details.get('missing_fields', [])}"
        )
        assert result.details["a11y_testable_cases"] > 0

    def test_real_cases_json_has_both_locale_testable_cases(self):
        """G2: 真实 generated_a11y_cases.json 必须包含可测试的 a11y 用例(供 zh-CN + en-US)。"""
        cases_json = precheck.CASES_JSON
        cases = json.loads(cases_json.read_text(encoding="utf-8"))
        a11y_cases = [c for c in cases if c.get("a11y_testable") and c.get("method") == "GET"]

        assert len(a11y_cases) > 0, "generated_a11y_cases.json 中无 a11y_testable GET 用例"
        # 每条 a11y_testable 用例应在 spec 中生成 zh-CN + en-US 两个测试
        # (验证测试矩阵非空,防止 0 用例假绿)

    def test_real_spec_file_has_no_stub_replacement(self):
        """G3: 真实 accessibility_behavior.spec.ts 必须不包含 stub replacement(假绿条件)。"""
        spec_content = precheck.SPEC_FILE.read_text(encoding="utf-8")

        # R62 P1-06 整改后,spec 文件不应包含 stub replacement
        assert "test = {" not in spec_content or "describe: () => {}" not in spec_content, (
            "accessibility_behavior.spec.ts 仍包含 stub replacement — "
            "R62 P1-06 整改未完成(依赖缺失会静默 skip = 假绿)"
        )
        # 不应包含 @ts-nocheck
        assert not spec_content.startswith("// @ts-nocheck"), (
            "accessibility_behavior.spec.ts 仍以 @ts-nocheck 开头 — "
            "R62 P1-06 整改要求移除 @ts-nocheck"
        )
        # 应包含直接 import(而非 try/catch require)
        assert "import { test, expect } from '@playwright/test'" in spec_content, (
            "accessibility_behavior.spec.ts 应使用直接 import "
            "(R62 P1-06: 依赖缺失时模块加载即抛错,不允许 stub 兜底)"
        )

    def test_real_spec_file_has_login_en_us_independent_locale(self):
        """G4: 真实 spec 文件必须为 /login en-US 实现独立 locale 初始化(不复用 zh-CN)。"""
        spec_content = precheck.SPEC_FILE.read_text(encoding="utf-8")

        # R62 P1-06: /login en-US 必须使用 Accept-Language + locale cookie 独立初始化
        assert "Accept-Language" in spec_content or "locale: 'en-US'" in spec_content, (
            "accessibility_behavior.spec.ts 必须为 /login en-US 实现 Accept-Language header "
            "或 locale cookie 独立初始化(R62 P1-06: 不复用 zh-CN 默认 locale 结果)"
        )
        # 不应包含旧的 skip 模式代码:`if (caze.path !== '/login')` 守卫跳过 locale 切换
        # (新实现用 `if (caze.path === '/login')` 分支独立处理 en-US)
        assert "if (caze.path !== '/login')" not in spec_content, (
            "accessibility_behavior.spec.ts 仍包含 `if (caze.path !== '/login')` 旧守卫 — "
            "R62 P1-06 整改要求 /login en-US 用 `if (caze.path === '/login')` 分支独立初始化 locale,"
            "不再跳过 /locale 切换导致 en-US 复用 zh-CN 结果(假绿)"
        )
        # 新实现应包含 `if (caze.path === '/login')` 分支
        assert "if (caze.path === '/login')" in spec_content, (
            "accessibility_behavior.spec.ts 应包含 `if (caze.path === '/login')` 分支 — "
            "R62 P1-06: /login en-US 必须独立处理(用 Accept-Language + locale cookie)"
        )
