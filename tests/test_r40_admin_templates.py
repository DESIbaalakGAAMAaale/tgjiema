"""R40 P0-1/P0-2/P1-13 整改验证测试。

测试范围:
- P0-1: 11 个新增 Admin 模板渲染(GET 路由返回 200,空数据场景)
- P0-2: AdminPrincipal 模型单元测试(verify_admin 返回 AdminPrincipal,
        路由中 int(admin) 不再抛 ValueError)
- P1-13: CSP nonce 注入模板上下文(每个响应包含 nonce,inline style/script 带 nonce)
- CSRF: POST 路由(takedown/maintenance)缺失 CSRF token 返回 403

测试策略:
- 在 import admin 之前注入 database/utils 的轻量 mock,绕过 CRDB 等重依赖
- 用 FastAPI TestClient + HTTPBasic 认证访问路由
- mock services.* 模块的异步函数,返回空数据验证渲染成功
"""
from __future__ import annotations

import hashlib
import secrets as _secrets
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── 在 import admin 之前注入轻量 mock,绕过重依赖 ───────────────
# conftest 已注入 config.settings(MagicMock),这里补充 admin 专用属性


def _ensure_admin_dependencies():
    """确保 admin 模块所需的重依赖已 mock。

    admin/__init__.py 在 import 时执行:
        from database import get_users_col, get_file_records_col, get_decode_logs_col
        from utils.monitor import metrics
        from config import settings
    需确保 database 和 utils.monitor 可导入。
    """
    # database: conftest 可能已构造轻量包,补齐 admin 需要的属性
    if "database" not in sys.modules:
        db = types.ModuleType("database")
        sys.modules["database"] = db
    db = sys.modules["database"]
    if not hasattr(db, "get_users_col"):
        db.get_users_col = MagicMock()
    if not hasattr(db, "get_file_records_col"):
        db.get_file_records_col = MagicMock()
    if not hasattr(db, "get_decode_logs_col"):
        db.get_decode_logs_col = MagicMock()
    if not hasattr(db, "init_db"):
        db.init_db = AsyncMock()
    if not hasattr(db, "close_db"):
        db.close_db = AsyncMock()

    # database.cache_store: admin startup 和路由需要
    if "database.cache_store" not in sys.modules:
        cs = types.ModuleType("database.cache_store")
        cs.get_cache_store = MagicMock(return_value=MagicMock())
        cs.get_all_bot_heartbeats = AsyncMock(return_value={})
        sys.modules["database.cache_store"] = cs
        setattr(db, "cache_store", cs)

    # utils 包
    if "utils" not in sys.modules:
        sys.modules["utils"] = types.ModuleType("utils")
    # utils.monitor
    if "utils.monitor" not in sys.modules:
        mon = types.ModuleType("utils.monitor")
        mon.metrics = MagicMock()
        mon.metrics.bots = {}
        mon.metrics.backup_count = 0
        mon.metrics.backup_fail_count = 0
        sys.modules["utils.monitor"] = mon
        setattr(sys.modules["utils"], "monitor", mon)
    # utils.shared_counters (dashboard 路由用)
    # 注意:mock 的 status_counters 必须含与真实模块一致的默认键(total_logs 等),
    # 否则会污染后续 test_r36_batch7_crdb_convergence.py::TestSharedCounters
    # (它断言 status_counters 默认含 total_logs=0)。
    if "utils.shared_counters" not in sys.modules:
        sc = types.ModuleType("utils.shared_counters")
        sc.status_counters = {
            "total_users": 0, "total_files": 0, "active_files": 0,
            "today_decodes": 0, "total_logs": 0,
        }
        sc.status_counters_initialized = True
        sc.status_counters_loaded_at = 0
        sys.modules["utils.shared_counters"] = sc
        setattr(sys.modules["utils"], "shared_counters", sc)


def _setup_admin_settings():
    """为 mock settings 注入 admin 专用配置。"""
    import config
    s = config.settings
    # 基础 admin 配置
    s.ADMIN_USERNAME = "admin"
    # 生成 PBKDF2 哈希密码(testpass -> $pbkdf2-sha256$...)
    salt = _secrets.token_bytes(16)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", b"testpass", salt, 200000)
    s.ADMIN_PASSWORD = f"$pbkdf2-sha256$200000${salt.hex()}${hash_bytes.hex()}"
    s.ADMIN_LOGIN_WINDOW = 300
    s.ADMIN_LOGIN_MAX_FAIL = 5
    s.ADMIN_COUNT_CACHE_TTL = 60
    s.ADMIN_SEARCH_MAX_LENGTH = 50
    s.ADMIN_PAGE_SIZE = 20
    s.ADMIN_FILES_PAGE_SIZE = 50
    s.CSRF_COOKIE_SECURE = False
    s.ADMIN_WEB_HOST = "127.0.0.1"
    s.ADMIN_WEB_PORT = 8080
    s.FREE_DAILY_QUOTA = 3
    s.BASIC_DAILY_QUOTA = 20
    s.PREMIUM_DAILY_QUOTA = -1
    s.FREE_EXTERNAL_DAILY_QUOTA = 0
    s.BASIC_EXTERNAL_DAILY_QUOTA = -1
    s.PREMIUM_EXTERNAL_DAILY_QUOTA = -1


_ensure_admin_dependencies()
_setup_admin_settings()

# 现在可以安全导入 admin
from admin import (  # noqa: E402
    AdminPrincipal,
    _get_admin_principal_id,
    _make_csrf_response,
    app,
    generate_password_hash,
)

from fastapi.testclient import TestClient  # noqa: E402

# ─── mock services 模块(路由内 lazy import) ─────────────────────


def _build_service_mocks():
    """构造所有 services.* mock 模块,返回 dict[name -> module]。"""
    mods = {}

    # services.task_center
    tc = types.ModuleType("services.task_center")
    tc.list_user_tasks = AsyncMock(return_value=[])
    # R40 P1-1: /tasks 路由改用 list_all_tasks(不带 user_id 过滤,查询所有用户)
    tc.list_all_tasks = AsyncMock(return_value=[])
    mods["services.task_center"] = tc

    # services.content_reports
    cr = types.ModuleType("services.content_reports")
    cr.list_reports = AsyncMock(return_value={"items": [], "total": 0})
    cr.get_report = AsyncMock(return_value=None)
    cr.takedown_content = AsyncMock(return_value=True)
    mods["services.content_reports"] = cr

    # services.collections
    col = types.ModuleType("services.collections")
    col.list_collections = AsyncMock(return_value={"items": [], "total": 0})
    mods["services.collections"] = col

    # services.notifications
    nt = types.ModuleType("services.notifications")
    nt.list_all_notifications = AsyncMock(return_value={"items": [], "total": 0})
    mods["services.notifications"] = nt

    # services.approval_workflow
    aw = types.ModuleType("services.approval_workflow")
    aw.list_pending = AsyncMock(return_value={"items": [], "total": 0})
    mods["services.approval_workflow"] = aw

    # services.rbac
    rb = types.ModuleType("services.rbac")
    rb.list_roles = AsyncMock(return_value=[])
    rb.list_permissions = AsyncMock(return_value=[])
    mods["services.rbac"] = rb

    # services.repair_console
    rc = types.ModuleType("services.repair_console")
    rc.get_repair_overview = AsyncMock(return_value={})
    rc.list_outbox = AsyncMock(return_value={"items": [], "total": 0})
    rc.list_dlq = AsyncMock(return_value={"items": [], "total": 0})
    rc.list_replication_failures = AsyncMock(return_value={"items": [], "total": 0})
    mods["services.repair_console"] = rc

    # services.topology_view
    tp = types.ModuleType("services.topology_view")
    tp.get_topology = AsyncMock(return_value={"accounts": [], "channels": []})
    tp.get_health_summary = AsyncMock(return_value={})
    mods["services.topology_view"] = tp

    # services.ru_cost_center
    ru = types.ModuleType("services.ru_cost_center")
    ru.get_daily_report = AsyncMock(return_value={})
    ru.check_ru_alert = AsyncMock(return_value=None)
    mods["services.ru_cost_center"] = ru

    # services.maintenance_mode
    mm = types.ModuleType("services.maintenance_mode")
    mm.get_status = AsyncMock(return_value={"enabled": False})
    mm.check_readiness = AsyncMock(return_value={})
    mm.enable = AsyncMock(return_value=True)
    mm.disable = AsyncMock(return_value=True)
    mods["services.maintenance_mode"] = mm

    # services.disaster_recovery
    dr = types.ModuleType("services.disaster_recovery")
    dr.list_backups = AsyncMock(return_value=[])
    dr.get_rpo_rto = AsyncMock(return_value={})
    dr.get_backup_schedule = AsyncMock(return_value={})
    mods["services.disaster_recovery"] = dr

    # services.command_bus (takedown/maintenance 路由 lazy import)
    # 不 mock 会导致 'services' is not a package 错误(因 services 被替换为非包 ModuleType)
    cb = types.ModuleType("services.command_bus")

    class _MockResult:
        success = True
        approval_required = False
        approval_id = 0
        error = ""
        data = None

    class _MockCommandBus:
        async def execute(self, command, principal):
            return _MockResult()

    cb.CommandBus = _MockCommandBus

    class _CBPrincipal:
        def __init__(self, id, name, source="web"):
            self.id = id
            self.name = name
            self.source = source

    cb.AdminPrincipal = _CBPrincipal
    cb.make_takedown_command = MagicMock()
    cb.make_enable_maintenance_command = MagicMock()
    cb.make_disable_maintenance_command = MagicMock()
    mods["services.command_bus"] = cb

    # services 包
    if "services" not in sys.modules:
        sys.modules["services"] = types.ModuleType("services")

    return mods


@pytest.fixture(autouse=True)
def _install_service_mocks():
    """每个用例前注入 services.* mock 模块,用例后还原。"""
    mods = _build_service_mocks()
    saved = {}
    for name, mod in mods.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
        # 也挂到 services 包上(lazy import 走 sys.modules 即可)
    yield
    for name, prev in saved.items():
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


# ─── 认证辅助 ──────────────────────────────────────────────────

AUTH = ("admin", "testpass")


def _client():
    """返回 TestClient(已进入 context,触发 startup)。"""
    return TestClient(app)


# ════════════════════════════════════════════════════════════════
# 1. AdminPrincipal 单元测试 (P0-2)
# ════════════════════════════════════════════════════════════════


class TestAdminPrincipal:
    """R40 P0-2: AdminPrincipal 身份模型。"""

    def test_principal_fields(self):
        p = AdminPrincipal(id=1, username="admin", roles=["super_admin"])
        assert p.id == 1
        assert p.username == "admin"
        assert p.roles == ["super_admin"]

    def test_principal_default_roles_empty(self):
        p = AdminPrincipal(id=2, username="root")
        assert p.roles == []

    def test_principal_id_stable_for_same_username(self):
        """同一 username 生成相同 id(幂等)。"""
        id1 = _get_admin_principal_id("admin")
        id2 = _get_admin_principal_id("admin")
        assert id1 == id2
        assert isinstance(id1, int)

    def test_principal_id_different_for_different_username(self):
        """不同 username 生成不同 id。"""
        id1 = _get_admin_principal_id("admin")
        id2 = _get_admin_principal_id("root")
        assert id1 != id2

    def test_principal_id_non_negative(self):
        """id 为非负整数(掩码 0x7FFFFFFF)。"""
        for name in ("admin", "root", "a", "very_long_username_123"):
            assert _get_admin_principal_id(name) >= 0

    def test_principal_id_empty_username_returns_zero(self):
        assert _get_admin_principal_id("") == 0

    def test_verify_admin_returns_principal(self):
        """verify_admin 返回 AdminPrincipal 而非字符串。"""
        from fastapi.security import HTTPBasicCredentials
        creds = HTTPBasicCredentials(username="admin", password="testpass")
        principal = None
        # 构造一个最小 request mock
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {}
        # 清空登录失败计数避免 429
        from admin import _login_failures
        _login_failures.clear()
        principal = None
        # 直接调用 verify_admin(同步函数)
        import admin as _admin_mod
        _admin_mod._login_failures.clear()
        principal = _admin_mod.verify_admin(credentials=creds, request=req)
        assert isinstance(principal, AdminPrincipal)
        assert principal.username == "admin"
        assert principal.id > 0
        assert "super_admin" in principal.roles

    def test_verify_admin_rejects_wrong_password(self):
        """错误密码返回 401。"""
        from fastapi.security import HTTPBasicCredentials
        from fastapi import HTTPException
        creds = HTTPBasicCredentials(username="admin", password="wrongpass")
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {}
        import admin as _admin_mod
        _admin_mod._login_failures.clear()
        with pytest.raises(HTTPException) as exc:
            _admin_mod.verify_admin(credentials=creds, request=req)
        assert exc.value.status_code == 401


# ════════════════════════════════════════════════════════════════
# 2. GET 路由渲染测试 (P0-1)
# ════════════════════════════════════════════════════════════════

# 11 个新增 R40 GET 路由
NEW_GET_ROUTES = [
    "/tasks",
    "/reports",
    "/collections",
    "/notifications",
    "/approvals",
    "/rbac",
    "/repair-console",
    "/topology",
    "/ru-cost",
    "/maintenance",
    "/disaster-recovery",
]


class TestAdminTemplatesRender:
    """R40 P0-1: 11 个新增模板渲染验证。"""

    @pytest.mark.parametrize("route", NEW_GET_ROUTES)
    def test_get_route_returns_200(self, route):
        """每个 GET 路由返回 200。"""
        with _client() as c:
            resp = c.get(route, auth=AUTH)
        assert resp.status_code == 200, f"{route} 返回 {resp.status_code}: {resp.text[:300]}"

    @pytest.mark.parametrize("route", NEW_GET_ROUTES)
    def test_get_route_has_csp_nonce(self, route):
        """每个响应的 CSP 头包含 nonce。"""
        with _client() as c:
            resp = c.get(route, auth=AUTH)
        csp = resp.headers.get("content-security-policy", "")
        assert "nonce-" in csp, f"{route} CSP 头缺少 nonce: {csp}"

    @pytest.mark.parametrize("route", NEW_GET_ROUTES)
    def test_get_route_has_csrf_token(self, route):
        """每个响应包含 CSRF token(cookie 设置成功)。

        GET 页面无数据时不渲染 POST 表单,csrf_token 仅在 cookie 中;
        有数据时 POST 表单含 hidden input。两种情况 cookie 都必须存在。
        """
        with _client() as c:
            resp = c.get(route, auth=AUTH)
        # CSRF cookie 必须存在(_make_csrf_response 设置)
        assert "csrf_token" in resp.cookies, f"{route} 响应缺少 csrf_token cookie"
        assert resp.cookies["csrf_token"], f"{route} csrf_token cookie 为空"

    @pytest.mark.parametrize("route", NEW_GET_ROUTES)
    def test_get_route_renders_empty_state(self, route):
        """空数据场景渲染成功(完整 HTML 页面 + '暂无数据' 或表单/表格)。"""
        with _client() as c:
            resp = c.get(route, auth=AUTH)
        assert resp.status_code == 200
        text = resp.text
        # 页面必须是完整 HTML(有 </html> 闭合)
        assert "</html>" in text, f"{route} 未渲染完整 HTML 页面"
        # 空数据时应含"暂无数据"、表格或表单(至少有内容区)
        assert ("暂无数据" in text or "<table" in text or "<form" in text
                or "暂无数据" in text), f"{route} 未渲染空数据状态"

    @pytest.mark.parametrize("route", NEW_GET_ROUTES)
    def test_get_route_has_security_headers(self, route):
        """每个响应包含安全头(X-Frame-Options, X-Content-Type-Options)。"""
        with _client() as c:
            resp = c.get(route, auth=AUTH)
        assert resp.headers.get("x-frame-options") == "DENY"
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_tasks_template_has_nonce_in_style(self):
        """P1-13: base.html 的 <style> 标签包含 nonce 属性。"""
        with _client() as c:
            resp = c.get("/tasks", auth=AUTH)
        # base.html 的 <style> 应带 nonce
        assert 'nonce="' in resp.text, "模板 inline style 缺少 nonce 属性"

    def test_reports_template_has_nonce_in_script(self):
        """P1-13: reports.html 的 inline <script> 标签包含 nonce 属性。"""
        with _client() as c:
            resp = c.get("/reports", auth=AUTH)
        text = resp.text
        # reports.html 含一个 <script nonce="...">
        assert '<script nonce="' in text, "reports.html inline script 缺少 nonce"


# ════════════════════════════════════════════════════════════════
# 3. POST 路由 CSRF 测试
# ════════════════════════════════════════════════════════════════


class TestPostRoutesCsrf:
    """POST 路由 CSRF 保护验证。"""

    def test_takedown_without_csrf_returns_403(self):
        """takedown 缺少 CSRF token 返回 403(而非 500)。"""
        with _client() as c:
            # 先 GET /reports 拿 cookie
            c.get("/reports", auth=AUTH)
            # POST 不带 csrf_token 字段(Form(...) 要求必填,缺失返回 422)
            # 但若带错误 token 应返回 403
            resp = c.post(
                "/reports/1/takedown",
                data={"csrf_token": "invalid_token"},
                auth=AUTH,
            )
        assert resp.status_code == 403, f"takedown 错误 CSRF 应返回 403,实际 {resp.status_code}"

    def test_takedown_nonexistent_report_returns_404(self):
        """takedown 不存在的举报返回 404(需要有效 CSRF)。"""
        with _client() as c:
            resp_get = c.get("/reports", auth=AUTH)
            token = resp_get.cookies.get("csrf_token", "")
            resp = c.post(
                "/reports/9999/takedown",
                data={"csrf_token": token},
                auth=AUTH,
            )
        assert resp.status_code == 404, f"takedown 不存在举报应返回 404,实际 {resp.status_code}"

    def test_maintenance_enable_without_csrf_returns_403(self):
        """maintenance enable 缺少有效 CSRF token 返回 403。"""
        with _client() as c:
            c.get("/maintenance", auth=AUTH)
            resp = c.post(
                "/maintenance/enable",
                data={"csrf_token": "invalid_token", "reason": "test"},
                auth=AUTH,
            )
        assert resp.status_code == 403, f"maintenance 错误 CSRF 应返回 403,实际 {resp.status_code}"

    def test_maintenance_enable_with_csrf_returns_redirect(self):
        """maintenance enable 带有效 CSRF token 返回 303 重定向。"""
        with _client() as c:
            resp_get = c.get("/maintenance", auth=AUTH)
            token = resp_get.cookies.get("csrf_token", "")
            resp = c.post(
                "/maintenance/enable",
                data={"csrf_token": token, "reason": "测试维护"},
                auth=AUTH,
                follow_redirects=False,
            )
        assert resp.status_code == 303, f"maintenance enable 应返回 303,实际 {resp.status_code}"

    def test_maintenance_disable_with_csrf_returns_redirect(self):
        """maintenance disable 带有效 CSRF token 返回 303。"""
        with _client() as c:
            resp_get = c.get("/maintenance", auth=AUTH)
            token = resp_get.cookies.get("csrf_token", "")
            resp = c.post(
                "/maintenance/disable",
                data={"csrf_token": token, "reason": "关闭"},
                auth=AUTH,
                follow_redirects=False,
            )
        assert resp.status_code == 303, f"maintenance disable 应返回 303,实际 {resp.status_code}"

    def test_takedown_with_csrf_no_500(self):
        """takedown 带 CSRF token 对非数字 admin 不再抛 ValueError/500。

        P0-2 核心验证:admin 用户名为 "admin"(非数字)时,
        takedown 不应因 int(admin) 抛 ValueError 产生 500。
        报告不存在应返回 404,而非 500。
        """
        with _client() as c:
            resp_get = c.get("/reports", auth=AUTH)
            token = resp_get.cookies.get("csrf_token", "")
            resp = c.post(
                "/reports/1/takedown",
                data={"csrf_token": token},
                auth=AUTH,
            )
        # 不应是 500(int(admin) ValueError 已修复)
        assert resp.status_code != 500, "takedown 触发 500,P0-2 int(admin) 未修复"
        # 应返回 404(报告不存在)
        assert resp.status_code == 404


# ════════════════════════════════════════════════════════════════
# 4. _make_csrf_response CSP nonce 注入测试 (P1-13)
# ════════════════════════════════════════════════════════════════


class TestCspNonceInjection:
    """R40 P1-13: CSP nonce 注入模板上下文。"""

    def test_make_csrf_response_injects_nonce(self):
        """_make_csrf_response 从 request.state 提取 csp_nonce 注入上下文。"""
        from admin import _make_csrf_response, _csrf_tokens
        req = MagicMock()
        req.state.csp_nonce = "test_nonce_12345"
        ctx = {"request": req}
        # 直接调用 _make_csrf_response(不走 HTTP)
        # 需要 mock templates.TemplateResponse
        import admin as _admin_mod
        orig = _admin_mod.templates.TemplateResponse
        try:
            captured = {}
            def fake_tr(name, context):
                captured["name"] = name
                captured["context"] = context
                resp = MagicMock()
                resp.set_cookie = MagicMock()
                return resp
            _admin_mod.templates.TemplateResponse = fake_tr
            _make_csrf_response("dummy.html", ctx, username="admin")
        finally:
            _admin_mod.templates.TemplateResponse = orig
        assert captured["context"]["csp_nonce"] == "test_nonce_12345"

    def test_make_csrf_response_nonce_empty_without_request(self):
        """无 request 时 csp_nonce 为空字符串(防御性降级)。"""
        import admin as _admin_mod
        orig = _admin_mod.templates.TemplateResponse
        try:
            captured = {}
            def fake_tr(name, context):
                captured["context"] = context
                resp = MagicMock()
                resp.set_cookie = MagicMock()
                return resp
            _admin_mod.templates.TemplateResponse = fake_tr
            _make_csrf_response("dummy.html", {}, username="admin")
        finally:
            _admin_mod.templates.TemplateResponse = orig
        assert captured["context"]["csp_nonce"] == ""


# ════════════════════════════════════════════════════════════════
# 5. 模板文件存在性检查 (P0-1)
# ════════════════════════════════════════════════════════════════


class TestTemplateFilesExist:
    """R40 P0-1: 11 个模板文件必须存在。"""

    REQUIRED_TEMPLATES = [
        "base.html",
        "tasks.html",
        "reports.html",
        "collections.html",
        "notifications.html",
        "approvals.html",
        "rbac.html",
        "repair_console.html",
        "topology.html",
        "ru_cost.html",
        "maintenance.html",
        "disaster_recovery.html",
    ]

    @pytest.mark.parametrize("tmpl", REQUIRED_TEMPLATES)
    def test_template_file_exists(self, tmpl):
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "admin" / "templates" / tmpl
        assert p.exists(), f"模板文件不存在: {p}"

    @pytest.mark.parametrize("tmpl", REQUIRED_TEMPLATES)
    def test_template_extends_base_or_has_nonce(self, tmpl):
        """新模板继承 base.html 或自带 nonce(P1-13)。"""
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "admin" / "templates" / tmpl
        content = p.read_text(encoding="utf-8")
        if tmpl == "base.html":
            # base.html 自身应包含 nonce
            assert 'nonce="{{ csp_nonce' in content, f"{tmpl} 缺少 CSP nonce"
        else:
            # 其他模板继承 base.html
            assert 'extends "base.html"' in content, f"{tmpl} 未继承 base.html"

    def test_post_forms_have_csrf_token(self):
        """含 POST 表单的模板必须包含 csrf_token hidden input。"""
        from pathlib import Path
        tmpl_dir = Path(__file__).resolve().parent.parent / "admin" / "templates"
        # reports.html 和 maintenance.html 含 POST 表单
        for tmpl in ("reports.html", "maintenance.html"):
            content = (tmpl_dir / tmpl).read_text(encoding="utf-8")
            assert 'name="csrf_token"' in content, f"{tmpl} POST 表单缺少 csrf_token"
