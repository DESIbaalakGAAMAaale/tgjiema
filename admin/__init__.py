import time as _time

from fastapi import FastAPI, Request, Depends, HTTPException, Query, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from pathlib import Path
import datetime

from database import get_users_col, get_file_records_col, get_decode_logs_col
from utils.monitor import metrics
from config import settings

app = FastAPI(title="TG解码器管理后台")
security = HTTPBasic()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ─── 登录速率限制 ──────────────────────────────────────────────
# IP -> [timestamps],记录 5 分钟内的失败时间戳
_login_failures: dict[str, list[float]] = {}
_LOGIN_LIMIT_WINDOW = 300   # 5 分钟
_LOGIN_LIMIT_MAX = 5        # 最多 5 次失败

# ─── CSRF 保护 ─────────────────────────────────────────────────
# 每个登录会话独立 token，防止跨站请求伪造
_csrf_tokens: dict[str, str] = {}

# M7: TTL 缓存 — 无筛选条件的 count_documents 走 CRDB 很贵，60s 缓存
# 仅缓存 {}, 有搜索条件时仍需 CRDB（regex 等无法缓存）
_count_cache: dict[str, tuple[float, int]] = {}
_COUNT_CACHE_TTL = 60  # 秒


def _get_csrf_token(username: str = "") -> str:
    """获取或生成当前会话的 CSRF token。"""
    if not username:
        return ""
    if username not in _csrf_tokens:
        _csrf_tokens[username] = secrets.token_hex(32)
    return _csrf_tokens[username]


def _verify_csrf(request: Request, form_token: str = None) -> bool:
    """验证 CSRF token:对比表单中的 csrf_token 和 cookie 中的 csrf_token。
    要求 cookie token 在服务端已注册且与表单 token 一致。"""
    cookie_token = request.cookies.get("csrf_token", "")
    if not cookie_token or not form_token:
        return False
    # cookie token 必须在服务端已注册（防止伪造），且必须与表单 token 一致
    if cookie_token in _csrf_tokens.values():
        return cookie_token == form_token
    return False


def verify_admin(credentials: HTTPBasicCredentials = Depends(security), request: Request = None):
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理员账号未配置,请在 .env 中设置 ADMIN_USERNAME 和 ADMIN_PASSWORD")

    # 速率限制检查
    client_ip = request.client.host if request else "unknown"
    now = _time.time()
    if client_ip not in _login_failures:
        _login_failures[client_ip] = []
    # 清理过期记录，key 为空则删除避免内存泄漏
    _login_failures[client_ip] = [
        ts for ts in _login_failures[client_ip]
        if now - ts < _LOGIN_LIMIT_WINDOW
    ]
    if not _login_failures[client_ip]:
        del _login_failures[client_ip]
    elif len(_login_failures[client_ip]) >= _LOGIN_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁,请 {_LOGIN_LIMIT_WINDOW // 60} 分钟后再试",
        )

    correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        settings.ADMIN_USERNAME.encode("utf8"),
    )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        settings.ADMIN_PASSWORD.encode("utf8"),
    )
    if not (correct_username and correct_password):
        # 记录失败
        _login_failures[client_ip].append(now)
        raise HTTPException(status_code=401, detail="未授权访问")

    # 登录成功,清除该 IP 的失败记录
    _login_failures.pop(client_ip, None)
    return credentials.username


def _make_csrf_response(template_name: str, context: dict, username: str = "") -> HTMLResponse:
    """生成带 CSRF cookie 的 HTML 响应。"""
    token = _get_csrf_token(username)
    context["csrf_token"] = token
    response = templates.TemplateResponse(template_name, context)
    response.set_cookie(
        key="csrf_token",
        value=token,
        httponly=True,
        samesite="strict",
        max_age=3600,
    )
    return response


@app.get("/health")
async def health_check(response: Response):
    """健康检查端点(无需认证),供 Docker healthcheck 和负载均衡器使用。
    读取 SQLite 共享心跳表，任一关键 Bot 离线时返回 503。
    """
    from database.cache_store import get_all_bot_heartbeats
    required = {"up", "idx", "dsp", "mon", "admin_bot"}
    beats = await get_all_bot_heartbeats()
    bot_status = {
        name: beats.get(name, {}).get("is_running", False)
        for name in required
    }
    healthy = all(bot_status.values())
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "bots": bot_status,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin=Depends(verify_admin)):
    import utils.shared_counters as _sc

    # 首次加载或 TTL 过期（60s）时从 SQLite 快照刷新计数器
    now = _time.time()
    if not _sc.status_counters_initialized or (now - _sc.status_counters_loaded_at > 60):
        from database.cache_store import get_cache_store
        store = get_cache_store()
        cached = await store.load_counter_snapshot()  # 0 RU，SQLite
        if cached and "total_users" in cached:
            for k, v in cached.items():
                _sc.status_counters[k] = v
        else:
            # SQLite 无数据时 CRDB 兜底
            users_col = get_users_col()
            files_col = get_file_records_col()
            _sc.status_counters["total_users"] = await users_col.count_documents({})
            _sc.status_counters["total_files"] = await files_col.count_documents({})
            _sc.status_counters["active_files"] = await files_col.count_documents({"status": "active"})
        _sc.status_counters_initialized = True
        _sc.status_counters_loaded_at = now

    total_users = _sc.status_counters.get("total_users", 0)
    total_files = _sc.status_counters.get("total_files", 0)
    active_files = _sc.status_counters.get("active_files", 0)

    today = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # today_decodes 需要日期过滤，从 CRDB 直查（计数器是累计值，不按天重置）
    logs_col = get_decode_logs_col()
    today_decodes = await logs_col.count_documents({"request_time": {"$gte": today}})

    bot_statuses = []
    for name, health in metrics.bots.items():
        bot_statuses.append(
            {
                "name": name,
                "is_running": health.is_running,
                "total_processed": health.total_processed,
                "total_errors": health.total_errors,
            }
        )

    return _make_csrf_response(
        "dashboard.html",
        {
            "request": request,
            "total_users": total_users,
            "total_files": total_files,
            "active_files": active_files,
            "today_decodes": today_decodes,
            "bot_statuses": bot_statuses,
            "send_success": metrics.send_success_count,
            "send_fail": metrics.send_fail_count,
            "backup_count": metrics.backup_count,
            "backup_fail": metrics.backup_fail_count,
        },
        username=admin,
    )


@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
    search: str = Query(""),
):
    per_page = 20
    users_col = get_users_col()

    query = {}
    if search:
        if search.isdigit():
            query["user_id"] = int(search)
        else:
            query["$or"] = [
                {"username": {"$regex": search, "$options": "i"}},
                {"first_name": {"$regex": search, "$options": "i"}},
            ]

    # M7: 无筛选条件时走 60s TTL 缓存，避免每次翻页都 count_documents CRDB
    if not search:
        now = _time.time()
        cached = _count_cache.get("users")
        if cached and (now - cached[0]) < _COUNT_CACHE_TTL:
            total = cached[1]
        else:
            total = await users_col.count_documents({})
            _count_cache["users"] = (now, total)
    else:
        total = await users_col.count_documents(query)
    skip = (page - 1) * per_page
    users = await users_col.find(query, sort=("created_at", -1), skip=skip, limit=per_page)

    return _make_csrf_response(
        "users.html",
        {
            "request": request,
            "users": users,
            "page": page,
            "total": total,
            "per_page": per_page,
            "search": search,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.post("/users/{user_id}/membership")
async def update_membership(
    user_id: int,
    request: Request,
    level: str = Query(...),
    csrf_token: str = Form(...),
    admin=Depends(verify_admin),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    if level not in ("free", "basic", "premium"):
        raise HTTPException(status_code=400, detail="无效的会员等级")

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    update = {
        "$set": {
            "membership_level": level,
            "updated_at": datetime.datetime.now(datetime.timezone.utc),
        }
    }
    if level == "free":
        update["$set"]["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
        update["$set"]["can_upload"] = True
        update["$set"]["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
        update["$set"]["external_used_today"] = 0
    elif level == "basic":
        update["$set"]["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
        update["$set"]["can_upload"] = True
        update["$set"]["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
        update["$set"]["external_used_today"] = 0
    elif level == "premium":
        update["$set"]["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
        update["$set"]["can_upload"] = True
        update["$set"]["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
        update["$set"]["external_used_today"] = 0

    await users_col.update_one({"user_id": user_id}, update)
    response = RedirectResponse(url="/users", status_code=303)
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin), httponly=True, samesite="strict", max_age=3600)
    return response


@app.post("/users/{user_id}/toggle_ban")
async def toggle_ban(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    admin=Depends(verify_admin),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_ban = not user.get("is_banned", False)
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": new_ban, "updated_at": datetime.datetime.now(datetime.timezone.utc)}},
    )
    response = RedirectResponse(url="/users", status_code=303)
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin), httponly=True, samesite="strict", max_age=3600)
    return response


@app.get("/files", response_class=HTMLResponse)
async def files_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
    search: str = Query(""),
):
    per_page = 20
    files_col = get_file_records_col()

    query = {}
    if search:
        if search.isdigit():
            query["uploader_id"] = int(search)
        else:
            query["file_code"] = {"$regex": search, "$options": "i"}

    # M7: 无筛选条件时走 60s TTL 缓存
    if not search:
        now = _time.time()
        cached = _count_cache.get("files")
        if cached and (now - cached[0]) < _COUNT_CACHE_TTL:
            total = cached[1]
        else:
            total = await files_col.count_documents({})
            _count_cache["files"] = (now, total)
    else:
        total = await files_col.count_documents(query)
    skip = (page - 1) * per_page
    files = await files_col.find(query, sort=("create_time", -1), skip=skip, limit=per_page)

    return _make_csrf_response(
        "files.html",
        {
            "request": request,
            "files": files,
            "page": page,
            "total": total,
            "per_page": per_page,
            "search": search,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.post("/files/{file_code}/delete")
async def delete_file(
    file_code: str,
    request: Request,
    csrf_token: str = Form(...),
    admin=Depends(verify_admin),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    files_col = get_file_records_col()
    result = await files_col.update_one(
        {"file_code": file_code},
        {"$set": {"status": "deleted"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="文件不存在")
    response = RedirectResponse(url="/files", status_code=303)
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin), httponly=True, samesite="strict", max_age=3600)
    return response


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
):
    per_page = 50
    logs_col = get_decode_logs_col()

    # M7: logs 无筛选条件，走 60s TTL 缓存
    now = _time.time()
    cached = _count_cache.get("logs")
    if cached and (now - cached[0]) < _COUNT_CACHE_TTL:
        total = cached[1]
    else:
        total = await logs_col.count_documents({})
        _count_cache["logs"] = (now, total)
    skip = (page - 1) * per_page
    logs = await logs_col.find(sort=("request_time", -1), skip=skip, limit=per_page)

    return _make_csrf_response(
        "logs.html",
        {
            "request": request,
            "logs": logs,
            "page": page,
            "total": total,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.get("/health-page", response_class=HTMLResponse)
async def health_page(request: Request, admin=Depends(verify_admin)):
    from database.cache_store import get_all_bot_heartbeats
    beats = await get_all_bot_heartbeats()
    bot_statuses = []
    for name, info in beats.items():
        bot_statuses.append(
            {
                "name": name,
                "is_running": info.get("is_running", False),
                "last_ping": info.get("last_ping", "N/A"),
                "total_processed": info.get("total_processed", 0),
                "total_errors": info.get("total_errors", 0),
            }
        )
    return _make_csrf_response(
        "health.html",
        {
            "request": request,
            "bot_statuses": bot_statuses,
        },
        username=admin,
    )