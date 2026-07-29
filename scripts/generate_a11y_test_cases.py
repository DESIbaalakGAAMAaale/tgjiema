#!/usr/bin/env python3
"""R61 P1-08: 从应用路由 + 元数据自动生成无障碍 e2e 测试用例。

取代人工 ``TEMPLATE_TO_ROUTE`` 数组:本脚本导入 ``admin.app`` 枚举全部已注册路由,
联查 ``admin.route_metadata.ROUTE_METADATA`` 派生测试用例描述符,
输出到 ``tests/e2e/generated_a11y_cases.json`` 作为 e2e 测试单一事实源。

用例描述符字段(每条 case):
  - path: 实际可访问的 URL(路径参数已用 param_fixtures 替换)
  - route_path: 路由模板(未替换参数,如 /users/{user_id}/membership)
  - method: HTTP 方法
  - template: Jinja2 模板文件名(无模板为 null)
  - param_fixtures: 路径参数 → fixture 值
  - permission: 所需权限
  - expected_landing: POST 重定向目标(GET 页面为 null)
  - is_locale_redirect: 是否为 locale 切换重定向端点
  - a11y_testable: 是否纳入无障碍页面测试矩阵
  - module: 路由 handler 所在模块

用法:
    python scripts/generate_a11y_test_cases.py
    python scripts/generate_a11y_test_cases.py --output path/to/cases.json

依赖: fastapi + admin 模块(导入触发路由注册,不启动服务)。
      本脚本独立运行时通过 ``_install_fake_config_if_missing`` 兜底,
      避免 admin import 因 ``Settings`` 校验失败(与 tests/conftest.py 同源逻辑)。
"""
from __future__ import annotations

import json
import re
import sys
import types
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_fake_config_if_missing() -> None:
    """若真实 config 不可用(环境变量缺失导致 Settings 校验失败),
    注入一个轻量 mock config 让 admin 模块可导入。

    本脚本需在 CI / 本地 / 测试环境统一运行,不能依赖 .env 完整配置。
    与 ``tests/conftest.py`` 的 ``_install_fake_config`` 同源逻辑(简化版)。
    """
    if "config" in sys.modules:
        # 已注入(如被 conftest 加载)— 检查 settings 是否真实可用
        try:
            cfg = sys.modules["config"]
            if hasattr(cfg, "settings") and cfg.settings is not None:
                return
        except Exception as _e:
            print(f"[WARN] _install_fake_config_if_missing: sys.modules['config'] check failed: {_e}", file=sys.stderr)
    try:
        import config  # noqa: F401  type: ignore
        if hasattr(config, "settings") and config.settings is not None:
            # 真实 config 可导入,不覆盖
            return
    except Exception as _e:
        print(f"[WARN] _install_fake_config_if_missing: config import failed: {_e}", file=sys.stderr)
    # 注入 mock config(Settings 校验失败时降级)
    from unittest.mock import MagicMock
    mock_settings = MagicMock(name="mock_settings_for_codegen")
    # 关键属性兜底(与 admin/__init__.py 读取的 settings 属性对齐)
    mock_settings.ADMIN_USERNAME = "admin"
    mock_settings.ADMIN_PASSWORD = "$pbkdf2-sha256$200000$salt$hash"  # PBKDF2 占位
    mock_settings.ADMIN_PRINCIPAL_ID = 1
    mock_settings.ADMIN_PRINCIPAL_USERNAME = "admin"
    mock_settings.ADMIN_PRINCIPAL_BOOTSTRAP_ROLES = "super_admin"
    mock_settings.ENVIRONMENT = "test"
    mock_settings.ADMIN_LOGIN_WINDOW = 300
    mock_settings.ADMIN_LOGIN_MAX_FAIL = 5
    mock_settings.ADMIN_COUNT_CACHE_TTL = 60
    mock_settings.ADMIN_SEARCH_MAX_LENGTH = 100
    mock_settings.ADMIN_PAGE_SIZE = 20
    mock_settings.ADMIN_FILES_PAGE_SIZE = 20
    mock_settings.FREE_DAILY_QUOTA = 10
    mock_settings.BASIC_DAILY_QUOTA = 100
    mock_settings.PREMIUM_DAILY_QUOTA = 1000
    mock_settings.FREE_EXTERNAL_DAILY_QUOTA = 5
    mock_settings.BASIC_EXTERNAL_DAILY_QUOTA = 50
    mock_settings.PREMIUM_EXTERNAL_DAILY_QUOTA = 500
    mock_settings.CSRF_COOKIE_SECURE = False
    mock_settings.BREAK_GLASS_PASSWORD = ""
    fake_config = types.ModuleType("config")
    fake_config.settings = mock_settings
    sys.modules["config"] = fake_config


def _install_telegram_mock_if_missing() -> None:
    """telegram 未安装时注入 MagicMock(admin 间接依赖)。

    与 tests/conftest.py 的 ``_install_telegram_mock_if_missing`` 同源逻辑。
    """
    try:
        import telegram  # noqa: F401  type: ignore
        return
    except ImportError:
        pass
    from unittest.mock import MagicMock
    sys.modules.setdefault("telegram", MagicMock(name="mock_telegram"))
    sys.modules.setdefault("telegram.ext", MagicMock(name="mock_telegram_ext"))


def _iter_admin_routes():
    """枚举 admin.app 已注册路由(过滤 FastAPI 框架内置路由)。

    与 ``scripts/export_admin_routes.py`` 同源逻辑,但本脚本直接消费路由 +
    route_metadata,不再单独导出 inventory。

    Yields:
        dict: 用例描述符(已用 param_fixtures 替换路径参数)
    """
    sys.path.insert(0, str(REPO_ROOT))
    _install_fake_config_if_missing()
    _install_telegram_mock_if_missing()
    import admin  # type: ignore
    from admin.route_metadata import get_metadata_index

    metadata_index = get_metadata_index()

    for route in admin.app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "") or "" if endpoint else ""
        # 过滤框架内置路由(/docs /openapi.json 等)
        if module.startswith(("fastapi", "starlette")):
            continue
        methods = getattr(route, "methods", None)
        method_list = sorted(methods) if methods else []
        for method in method_list:
            if method == "HEAD":
                # HEAD 与 GET 同路径,不单独生成用例
                continue
            meta = metadata_index.get((method, path))
            if meta is None:
                # 路由未在 ROUTE_METADATA 声明 — 报告缺失,跳过
                # (供 CI 检查:新增路由必须同步更新 route_metadata)
                print(
                    f"[generate_a11y_test_cases] WARN: 路由未声明元数据,跳过: "
                    f"{method} {path}",
                    file=sys.stderr,
                )
                continue
            yield _build_case(method, path, module, meta)


def _build_case(method: str, route_path: str, module: str, meta: dict) -> dict:
    """根据路由模板 + 元数据构建测试用例描述符。"""
    case = deepcopy(meta)
    case["route_path"] = route_path
    case["method"] = method
    case["module"] = module
    # 用 param_fixtures 替换路径参数占位符,生成实际可访问的 URL
    case["path"] = _resolve_path(route_path, meta.get("param_fixtures") or {})
    return case


_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _resolve_path(route_path: str, param_fixtures: dict) -> str:
    """将路由模板中的 ``{param}`` 占位符替换为 fixture 值。

    例: ``/users/{user_id}/membership`` + ``{"user_id": 1}`` → ``/users/1/membership``
    若 fixture 缺失,保留占位符并警告(测试用例不应直接访问未替换的占位符)。
    """
    def repl(m: re.Match) -> str:
        param = m.group(1)
        if param in param_fixtures:
            return str(param_fixtures[param])
        # 缺失 fixture — 保留占位符(测试侧应跳过或补 fixture)
        return m.group(0)

    return _PATH_PARAM_RE.sub(repl, route_path)


# 用例描述符的字段顺序(输出 JSON 时保持稳定,便于 diff)
_CASE_FIELDS = (
    "path",
    "route_path",
    "method",
    "template",
    "param_fixtures",
    "permission",
    "expected_landing",
    "is_locale_redirect",
    "a11y_testable",
    "module",
)


def _normalize_case(case: dict) -> dict:
    """按 ``_CASE_FIELDS`` 顺序整理字段,便于稳定 diff。"""
    return {k: case.get(k) for k in _CASE_FIELDS}


def main() -> int:
    output_path: Path | None = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = Path(sys.argv[idx + 1])
    cases = [_normalize_case(c) for c in _iter_admin_routes()]
    # 稳定排序:(method, path)
    cases.sort(key=lambda c: (c["method"], c["path"]))
    payload = json.dumps(cases, ensure_ascii=False, indent=2)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
        print(
            f"✓ R61 P1-08: 已生成 {len(cases)} 条无障碍测试用例 → {output_path}",
            file=sys.stderr,
        )
        a11y_count = sum(1 for c in cases if c.get("a11y_testable"))
        locale_count = sum(1 for c in cases if c.get("is_locale_redirect"))
        print(
            f"  其中 a11y_testable={a11y_count} locale_redirect={locale_count}",
            file=sys.stderr,
        )
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
