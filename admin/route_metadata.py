"""R61 P1-08: 路由元数据声明(template / param fixture / permission / expected landing)。

供 ``scripts/generate_a11y_test_cases.py`` 消费,自动生成无障碍 e2e 测试用例。
取代人工 ``TEMPLATE_TO_ROUTE`` 数组:从应用路由 + 本元数据派生测试矩阵,
新增 admin 路由必须同步在此声明,否则生成器会跳过并打印警告(供 CI 检查)。

字段说明(每条 ROUTE_METADATA 项):
  - path: 路由模板(与 FastAPI ``@app.get``/``@app.post`` 装饰器路径一致,
          含路径参数占位符如 ``{user_id}``)
  - method: HTTP 方法(GET/POST)
  - template: 渲染所用 Jinja2 模板文件名(无模板的 JSON/重定向端点为 None)
  - param_fixtures: 路径参数 → 测试 fixture 值(如 ``{"user_id": 1}``),
                    用于生成实际可访问的测试 URL
  - permission: 所需权限/角色(public / require_session)
  - expected_landing: POST 重定向后的目标路径(GET 页面为 None)
  - is_locale_redirect: 是否为 locale 切换重定向端点(如 ``/locale``)
  - a11y_testable: 是否纳入无障碍页面测试矩阵(HTML 页面为 True;
                   JSON/重定向/POST 端点为 False)

未声明的路由(如 ``/docs`` ``/openapi.json`` ``/redoc`` ``/docs/oauth2-redirect``
等 FastAPI 框架内置路由)由 ``generate_a11y_test_cases.py`` 过滤,不纳入 a11y 测试范围。
"""
from __future__ import annotations

# 路由元数据 — 单一事实源,供 generate_a11y_test_cases.py 派生测试用例
# 键: (method, path),与 admin.app 路由注册一致
ROUTE_METADATA: list[dict] = [
    # ─── 认证 / MFA / locale ────────────────────────────────────
    {
        "path": "/login",
        "method": "GET",
        "template": None,  # 内联 HTML(_i18n_t('admin.__init__.s1'))
        "param_fixtures": {},
        "permission": "public",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,  # 登录页是无障碍关键页面
    },
    {
        "path": "/login",
        "method": "POST",
        "template": None,
        "param_fixtures": {},
        "permission": "public",
        "expected_landing": "/",  # 登录成功重定向到首页(MFA 未启用时)
        "is_locale_redirect": False,
        "a11y_testable": False,  # POST 表单提交,非页面加载
    },
    {
        "path": "/login/mfa",
        "method": "POST",
        "template": None,
        "param_fixtures": {},
        "permission": "public",
        "expected_landing": "/",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/logout",
        "method": "POST",
        "template": None,
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": "/login",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/break-glass/login",
        "method": "POST",
        "template": None,
        "param_fixtures": {},
        "permission": "public",
        "expected_landing": None,  # 返回 JSON(非重定向)
        "is_locale_redirect": False,
        "a11y_testable": False,  # JSON 响应,本机限制
    },
    {
        "path": "/mfa/setup",
        "method": "GET",
        "template": None,  # 内联 HTML(_i18n_t('admin.__init__.s3'))
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/mfa/setup",
        "method": "POST",
        "template": None,
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": "/",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/mfa/disable",
        "method": "POST",
        "template": None,
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": "/",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    # ─── 基础设施端点(非 HTML 页面) ────────────────────────────
    {
        "path": "/health",
        "method": "GET",
        "template": None,
        "param_fixtures": {},
        "permission": "public",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": False,  # JSON 健康检查
    },
    {
        "path": "/readiness",
        "method": "GET",
        "template": None,
        "param_fixtures": {},
        "permission": "public",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": False,  # JSON readiness 探针
    },
    # ─── locale 切换端点(重定向) ───────────────────────────────
    {
        "path": "/locale",
        "method": "GET",
        "template": None,
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,  # 动态:Referer 或 "/"
        "is_locale_redirect": True,
        "a11y_testable": False,  # 重定向端点,由 assertLocaleRedirect 专项测试
    },
    # ─── 管理页面(GET HTML 页面 — a11y 测试矩阵核心) ──────────
    {
        "path": "/",
        "method": "GET",
        "template": "dashboard.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/users",
        "method": "GET",
        "template": "users.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/files",
        "method": "GET",
        "template": "files.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/logs",
        "method": "GET",
        "template": "logs.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/health-page",
        "method": "GET",
        "template": "health.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/tasks",
        "method": "GET",
        "template": "tasks.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/reports",
        "method": "GET",
        "template": "reports.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/collections",
        "method": "GET",
        "template": "collections.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/notifications",
        "method": "GET",
        "template": "notifications.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/approvals",
        "method": "GET",
        "template": "approvals.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/rbac",
        "method": "GET",
        "template": "rbac.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/repair-console",
        "method": "GET",
        "template": "repair_console.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/topology",
        "method": "GET",
        "template": "topology.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/ru-cost",
        "method": "GET",
        "template": "ru_cost.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/maintenance",
        "method": "GET",
        "template": "maintenance.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    {
        "path": "/disaster-recovery",
        "method": "GET",
        "template": "disaster_recovery.html",
        "param_fixtures": {},
        "permission": "require_session",
        "expected_landing": None,
        "is_locale_redirect": False,
        "a11y_testable": True,
    },
    # ─── POST 操作路由(重定向到对应 GET 页面) ─────────────────
    {
        "path": "/users/{user_id}/membership",
        "method": "POST",
        "template": None,
        "param_fixtures": {"user_id": 1},
        "permission": "require_session",
        "expected_landing": "/users",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/users/{user_id}/toggle_ban",
        "method": "POST",
        "template": None,
        "param_fixtures": {"user_id": 1},
        "permission": "require_session",
        "expected_landing": "/users",  # 或 /approvals(需审批时)
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/files/{file_code}/delete",
        "method": "POST",
        "template": None,
        "param_fixtures": {"file_code": "test-fixture-code"},
        "permission": "require_session",
        "expected_landing": "/files",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/reports/{report_id}/takedown",
        "method": "POST",
        "template": None,
        "param_fixtures": {"report_id": 1},
        "permission": "require_session",
        "expected_landing": "/reports",  # 或 /approvals(需审批时)
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
    {
        "path": "/maintenance/{action}",
        "method": "POST",
        "template": None,
        "param_fixtures": {"action": "disable"},
        "permission": "require_session",
        "expected_landing": "/maintenance",
        "is_locale_redirect": False,
        "a11y_testable": False,
    },
]


def get_route_metadata() -> list[dict]:
    """返回路由元数据列表(深拷贝避免外部修改)。"""
    import copy
    return copy.deepcopy(ROUTE_METADATA)


def get_metadata_index() -> dict[tuple[str, str], dict]:
    """返回以 (method, path) 为键的元数据索引,便于 O(1) 查找。"""
    return {(item["method"], item["path"]): item for item in ROUTE_METADATA}
