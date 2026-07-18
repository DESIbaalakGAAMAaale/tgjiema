"""R61 P1-08: 无障碍路由自动代码生成测试。

审计 P1-08: 无障碍路由仍依赖人工 TEMPLATE_TO_ROUTE。
整改: 从应用路由 + 元数据自动生成 a11y 测试用例,取代人工数组。

测试覆盖:
    A. 生成器覆盖全部已声明路由(admin.app 注册路由 ⊆ 生成用例)
    B. 路径参数 fixture 全部填充(无未替换的 {param} 占位符)
    C. 重定向路由(expected_landing 非空)的 a11y_testable 为 False
    D. locale 重定向路由(is_locale_redirect=True)正确标记
    E. 所有 a11y_testable=True 的用例声明了 template(HTML 页面)
    F. route_metadata 覆盖全部 admin 业务路由(无未声明路由)
    G. 生成器输出可序列化为 JSON(e2e 测试侧可加载)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 让测试能导入 scripts/generate_a11y_test_cases.py
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ─── 辅助函数 ──────────────────────────────────────────────────

def _run_generator() -> list[dict]:
    """运行生成器,返回用例列表(归一化 + 排序后,与 main() 输出一致)。"""
    import generate_a11y_test_cases as gen
    cases = list(gen._iter_admin_routes())
    cases = [gen._normalize_case(c) for c in cases]
    cases.sort(key=lambda c: (c["method"], c["path"]))
    return cases


def _get_admin_routes() -> set[tuple[str, str]]:
    """返回 admin.app 注册的业务路由集合(过滤框架内置路由)。

    Returns:
        {(method, path), ...}
    """
    import generate_a11y_test_cases as gen
    # 复用生成器的环境初始化逻辑(mock config + telegram)
    gen._install_fake_config_if_missing()
    gen._install_telegram_mock_if_missing()
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import admin  # type: ignore
    routes: set[tuple[str, str]] = set()
    for route in admin.app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "") or "" if endpoint else ""
        if module.startswith(("fastapi", "starlette")):
            continue
        methods = getattr(route, "methods", None)
        method_list = sorted(methods) if methods else []
        for method in method_list:
            if method == "HEAD":
                continue
            routes.add((method, path))
    return routes


# ════════════════════════════════════════════════════════════════
# A. 生成器覆盖全部已声明路由
# ════════════════════════════════════════════════════════════════


class TestGeneratorCoversAllRoutes:
    """A 节: 生成器输出覆盖全部 admin 业务路由。"""

    def test_generator_outputs_cases_for_all_admin_routes(self):
        """A1: admin.app 注册的每条业务路由都对应一条生成用例。"""
        cases = _run_generator()
        admin_routes = _get_admin_routes()
        case_routes = {(c["method"], c["route_path"]) for c in cases}
        missing = admin_routes - case_routes
        assert not missing, (
            f"以下 admin 路由未生成用例(需在 route_metadata 中声明): "
            f"{sorted(missing)}"
        )

    def test_generator_case_count_matches_metadata(self):
        """A2: 生成用例数 == ROUTE_METADATA 声明数(无静默丢失)。"""
        from admin.route_metadata import ROUTE_METADATA
        cases = _run_generator()
        assert len(cases) == len(ROUTE_METADATA), (
            f"生成用例数 {len(cases)} != 元数据声明数 {len(ROUTE_METADATA)}"
        )

    def test_generator_case_count_positive(self):
        """A3: 生成器产出非空(至少有 /login 页面用例 + / dashboard 用例)。"""
        cases = _run_generator()
        assert len(cases) > 0, "生成器未输出任何用例"
        paths = {c["route_path"] for c in cases}
        assert "/login" in paths, "缺少 /login 路由用例"
        assert "/" in paths, "缺少 / dashboard 路由用例"


# ════════════════════════════════════════════════════════════════
# B. 路径参数 fixture 填充
# ════════════════════════════════════════════════════════════════


class TestParamFixturesPopulated:
    """B 节: 路径参数 fixture 必须填充(无未替换的 {param} 占位符)。"""

    def test_no_unresolved_path_placeholders(self):
        """B1: 用例 path 字段不含未替换的 {param} 占位符。"""
        cases = _run_generator()
        placeholder_re = re.compile(r"\{[^}]+\}")
        unresolved = [
            (c["method"], c["path"]) for c in cases
            if placeholder_re.search(c["path"])
        ]
        assert not unresolved, (
            f"以下用例 path 仍含未替换的占位符(需补 param_fixtures): {unresolved}"
        )

    def test_routes_with_path_params_have_fixtures(self):
        """B2: route_path 含 {param} 的用例必须声明非空 param_fixtures,
        且每个 {param} 都在 fixtures 中。
        """
        cases = _run_generator()
        param_re = re.compile(r"\{([^}]+)\}")
        for c in cases:
            if param_re.search(c["route_path"]):
                assert c["param_fixtures"], (
                    f"路由 {c['method']} {c['route_path']} 含路径参数但 "
                    f"param_fixtures 为空"
                )
                # 每个 {param} 都应在 fixtures 中
                params_in_path = {
                    p.strip("{}") for p in param_re.findall(c["route_path"])
                }
                fixture_keys = set(c["param_fixtures"].keys())
                missing = params_in_path - fixture_keys
                assert not missing, (
                    f"路由 {c['route_path']} 参数 {missing} 未在 param_fixtures "
                    f"中声明(fixture_keys={fixture_keys})"
                )

    def test_specific_fixture_values_populated(self):
        """B3: 关键路由的 fixture 值正确填充(抽样验证)。"""
        cases = _run_generator()
        case_by_route = {(c["method"], c["route_path"]): c for c in cases}
        # /users/{user_id}/membership → user_id 已替换
        c = case_by_route.get(("POST", "/users/{user_id}/membership"))
        assert c is not None, "缺少 /users/{user_id}/membership 用例"
        assert c["param_fixtures"].get("user_id") == 1
        assert c["path"] == "/users/1/membership"
        # /files/{file_code}/delete → file_code 已替换
        c = case_by_route.get(("POST", "/files/{file_code}/delete"))
        assert c is not None, "缺少 /files/{file_code}/delete 用例"
        assert "file_code" in c["param_fixtures"]
        assert c["path"].startswith("/files/")
        assert c["path"].endswith("/delete")
        assert "{file_code}" not in c["path"]


# ════════════════════════════════════════════════════════════════
# C. 重定向路由的 a11y_testable 标记
# ════════════════════════════════════════════════════════════════


class TestRedirectCasesMarked:
    """C 节: 重定向路由(expected_landing 非空)的 a11y_testable 必须为 False。"""

    def test_redirect_routes_not_a11y_testable(self):
        """C1: expected_landing 非空 → a11y_testable 为 False。"""
        cases = _run_generator()
        for c in cases:
            if c.get("expected_landing"):
                assert not c.get("a11y_testable"), (
                    f"路由 {c['method']} {c['route_path']} 有 expected_landing="
                    f"{c['expected_landing']} 但 a11y_testable=True"
                    f"(POST 重定向非页面加载,不应纳入 a11y 页面测试矩阵)"
                )

    def test_post_routes_have_expected_landing_or_skipped(self):
        """C2: POST 路由必须声明 expected_landing(重定向目标)或为非重定向(JSON)。"""
        cases = _run_generator()
        post_cases = [c for c in cases if c["method"] == "POST"]
        assert len(post_cases) > 0, "无 POST 用例(异常)"
        for c in post_cases:
            # POST 路由要么有 expected_landing(重定向),
            # 要么 a11y_testable=False(JSON/非页面)
            if not c.get("expected_landing"):
                assert not c.get("a11y_testable"), (
                    f"POST 路由 {c['route_path']} 无 expected_landing 但 "
                    f"a11y_testable=True"
                )

    def test_expected_landing_for_redirect_routes(self):
        """C3: 重定向路由的 expected_landing 字段已设置(抽样验证)。"""
        cases = _run_generator()
        case_by_route = {(c["method"], c["route_path"]): c for c in cases}
        # /logout → /login
        c = case_by_route.get(("POST", "/logout"))
        assert c is not None
        assert c["expected_landing"] == "/login"
        # /login POST → /
        c = case_by_route.get(("POST", "/login"))
        assert c is not None
        assert c["expected_landing"] == "/"
        # /users/{user_id}/membership → /users
        c = case_by_route.get(("POST", "/users/{user_id}/membership"))
        assert c is not None
        assert c["expected_landing"] == "/users"


# ════════════════════════════════════════════════════════════════
# D. locale 重定向路由标记
# ════════════════════════════════════════════════════════════════


class TestLocaleRedirectMarked:
    """D 节: locale 重定向路由(/locale)正确标记。"""

    def test_locale_route_marked(self):
        """D1: /locale 路由的 is_locale_redirect=True,a11y_testable=False。"""
        cases = _run_generator()
        locale_cases = [c for c in cases if c["route_path"] == "/locale"]
        assert len(locale_cases) == 1, (
            f"期望 1 条 /locale 用例,实际 {len(locale_cases)}"
        )
        c = locale_cases[0]
        assert c["is_locale_redirect"] is True
        assert c["a11y_testable"] is False
        assert c["method"] == "GET"

    def test_only_locale_route_is_locale_redirect(self):
        """D2: 只有 /locale 路由的 is_locale_redirect=True(无其他路由误标)。"""
        cases = _run_generator()
        locale_redirect_cases = [c for c in cases if c.get("is_locale_redirect")]
        assert len(locale_redirect_cases) == 1, (
            f"期望只有 /locale 是 locale 重定向,实际: "
            f"{[c['route_path'] for c in locale_redirect_cases]}"
        )
        assert locale_redirect_cases[0]["route_path"] == "/locale"


# ════════════════════════════════════════════════════════════════
# E. a11y_testable 用例必须有 template
# ════════════════════════════════════════════════════════════════


class TestA11yTestableCasesHaveTemplate:
    """E 节: 所有 a11y_testable=True 的用例必须有 template(HTML 页面)。

    例外: /login 和 /mfa/setup 使用内联 HTML(无模板文件),template=None
    但 a11y_testable=True。
    """

    # 允许 template=None 但 a11y_testable=True 的路由(内联 HTML 页面)
    INLINE_HTML_ROUTES = {("GET", "/login"), ("GET", "/mfa/setup")}

    def test_a11y_testable_cases_have_template_or_inline(self):
        """E1: a11y_testable=True 的用例必须有 template 或在 INLINE_HTML_ROUTES 白名单。"""
        cases = _run_generator()
        for c in cases:
            if c.get("a11y_testable"):
                key = (c["method"], c["route_path"])
                if key in self.INLINE_HTML_ROUTES:
                    continue  # 内联 HTML 页面,允许 template=None
                assert c.get("template"), (
                    f"路由 {key} 标记 a11y_testable=True 但 template 为空"
                )

    def test_template_files_exist(self):
        """E2: 声明了 template 的用例,模板文件必须在 admin/templates/ 下存在。"""
        cases = _run_generator()
        templates_dir = REPO_ROOT / "admin" / "templates"
        assert templates_dir.exists(), f"模板目录不存在: {templates_dir}"
        for c in cases:
            tpl = c.get("template")
            if not tpl:
                continue
            tpl_path = templates_dir / tpl
            assert tpl_path.exists(), (
                f"路由 {c['method']} {c['route_path']} 声明 template={tpl} "
                f"但文件不存在: {tpl_path}"
            )

    def test_a11y_testable_count_covers_all_html_pages(self):
        """E3: a11y_testable=True 的用例数应覆盖全部 16 个 HTML 页面模板 +
        /login + /mfa/setup = 18(与 generated_a11y_cases.json 一致)。
        """
        cases = _run_generator()
        a11y_cases = [c for c in cases if c.get("a11y_testable")]
        # 16 个模板 HTML + /login(内联)+ /mfa/setup(内联)= 18
        assert len(a11y_cases) == 18, (
            f"a11y_testable=True 用例数期望 18,实际 {len(a11y_cases)}: "
            f"{[c['route_path'] for c in a11y_cases]}"
        )


# ════════════════════════════════════════════════════════════════
# F. route_metadata 覆盖全部 admin 业务路由(无未声明路由)
# ════════════════════════════════════════════════════════════════


class TestRouteMetadataCoverage:
    """F 节: route_metadata 覆盖全部 admin 业务路由(无未声明 / 无过期)。"""

    def test_metadata_covers_all_admin_routes(self):
        """F1: admin.app 每条业务路由都在 ROUTE_METADATA 中声明。"""
        from admin.route_metadata import ROUTE_METADATA
        admin_routes = _get_admin_routes()
        declared = {(m["method"], m["path"]) for m in ROUTE_METADATA}
        undeclared = admin_routes - declared
        assert not undeclared, (
            f"以下 admin 路由未在 route_metadata 中声明(需补充): "
            f"{sorted(undeclared)}"
        )

    def test_metadata_no_stale_routes(self):
        """F2: ROUTE_METADATA 中没有已删除的 admin 路由(防止过期)。"""
        from admin.route_metadata import ROUTE_METADATA
        admin_routes = _get_admin_routes()
        declared = {(m["method"], m["path"]) for m in ROUTE_METADATA}
        stale = declared - admin_routes
        assert not stale, (
            f"ROUTE_METADATA 中存在已删除的 admin 路由(需清理): "
            f"{sorted(stale)}"
        )

    def test_metadata_no_duplicates(self):
        """F3: ROUTE_METADATA 中 (method, path) 无重复。"""
        from admin.route_metadata import ROUTE_METADATA
        seen = set()
        duplicates = []
        for m in ROUTE_METADATA:
            key = (m["method"], m["path"])
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        assert not duplicates, f"ROUTE_METADATA 中存在重复: {duplicates}"

    def test_metadata_required_fields_present(self):
        """F4: 每条 ROUTE_METADATA 项包含所有必需字段。"""
        from admin.route_metadata import ROUTE_METADATA
        required_fields = {
            "path", "method", "template", "param_fixtures",
            "permission", "expected_landing", "is_locale_redirect",
            "a11y_testable",
        }
        for i, m in enumerate(ROUTE_METADATA):
            missing = required_fields - set(m.keys())
            assert not missing, (
                f"ROUTE_METADATA[{i}] ({m.get('method')} {m.get('path')}) "
                f"缺少字段: {missing}"
            )


# ════════════════════════════════════════════════════════════════
# G. 生成器输出可序列化为 JSON
# ════════════════════════════════════════════════════════════════


class TestGeneratorJSONOutput:
    """G 节: 生成器输出可序列化为 JSON(e2e 测试侧可加载)。"""

    def test_cases_serializable_to_json(self):
        """G1: 生成用例可序列化为 JSON(无 set/datetime 等不可序列化类型)。"""
        cases = _run_generator()
        payload = json.dumps(cases, ensure_ascii=False, indent=2)
        assert isinstance(payload, str)
        # 反序列化验证
        loaded = json.loads(payload)
        assert len(loaded) == len(cases)

    def test_main_writes_output_file(self, tmp_path):
        """G2: main() --output 参数可将用例写入文件。"""
        import generate_a11y_test_cases as gen
        out_file = tmp_path / "cases.json"
        old_argv = sys.argv
        try:
            sys.argv = ["generate_a11y_test_cases.py", "--output", str(out_file)]
            rc = gen.main()
        finally:
            sys.argv = old_argv
        assert rc == 0
        assert out_file.exists()
        loaded = json.loads(out_file.read_text(encoding="utf-8"))
        assert len(loaded) > 0
        # 每条用例包含必需字段
        required_fields = {
            "path", "route_path", "method", "template",
            "param_fixtures", "permission", "expected_landing",
            "is_locale_redirect", "a11y_testable",
        }
        for c in loaded:
            missing = required_fields - set(c.keys())
            assert not missing, f"用例 {c.get('route_path')} 缺少字段: {missing}"

    def test_main_stdout_outputs_json_when_no_output_arg(self, capsys):
        """G3: main() 无 --output 参数时,JSON 输出到 stdout(供测试侧 JSON.parse)。"""
        import generate_a11y_test_cases as gen
        old_argv = sys.argv
        try:
            sys.argv = ["generate_a11y_test_cases.py"]
            rc = gen.main()
        finally:
            sys.argv = old_argv
        assert rc == 0
        captured = capsys.readouterr()
        loaded = json.loads(captured.out)
        assert isinstance(loaded, list)
        assert len(loaded) > 0

    def test_generated_cases_file_exists_in_repo(self):
        """G4: tests/e2e/generated_a11y_cases.json 已生成并提交到仓库。"""
        cases_file = REPO_ROOT / "tests" / "e2e" / "generated_a11y_cases.json"
        assert cases_file.exists(), (
            f"generated_a11y_cases.json 不存在,请运行: "
            f"python scripts/generate_a11y_test_cases.py "
            f"--output tests/e2e/generated_a11y_cases.json"
        )
        loaded = json.loads(cases_file.read_text(encoding="utf-8"))
        assert isinstance(loaded, list)
        assert len(loaded) > 0
        # 与生成器实时输出一致(无漂移)
        live_cases = _run_generator()
        assert len(loaded) == len(live_cases), (
            f"generated_a11y_cases.json({len(loaded)} 条)与生成器实时输出"
            f"({len(live_cases)} 条)不一致,请重新运行生成器"
        )


# ════════════════════════════════════════════════════════════════
# H. _resolve_path 单元测试
# ════════════════════════════════════════════════════════════════


class TestResolvePath:
    """H 节: _resolve_path 路径参数替换逻辑单元测试。"""

    def test_resolve_no_params(self):
        """H1: 无路径参数 → 原样返回。"""
        import generate_a11y_test_cases as gen
        assert gen._resolve_path("/users", {}) == "/users"
        assert gen._resolve_path("/", {}) == "/"

    def test_resolve_single_param(self):
        """H2: 单路径参数 → 替换为 fixture 值。"""
        import generate_a11y_test_cases as gen
        assert gen._resolve_path("/users/{user_id}/membership", {"user_id": 1}) == "/users/1/membership"
        assert gen._resolve_path("/files/{file_code}/delete", {"file_code": "abc"}) == "/files/abc/delete"

    def test_resolve_multiple_params(self):
        """H3: 多路径参数 → 全部替换。"""
        import generate_a11y_test_cases as gen
        result = gen._resolve_path(
            "/a/{x}/b/{y}/c", {"x": "X", "y": "Y"}
        )
        assert result == "/a/X/b/Y/c"

    def test_resolve_missing_fixture_keeps_placeholder(self):
        """H4: fixture 缺失 → 保留占位符(不抛错,由上层警告)。"""
        import generate_a11y_test_cases as gen
        result = gen._resolve_path("/users/{user_id}", {})
        assert result == "/users/{user_id}"

    def test_resolve_string_and_int_fixtures(self):
        """H5: fixture 值可为 int 或 str(int 被 str() 转换)。"""
        import generate_a11y_test_cases as gen
        assert gen._resolve_path("/u/{id}", {"id": 123}) == "/u/123"
        assert gen._resolve_path("/u/{id}", {"id": "abc"}) == "/u/abc"
