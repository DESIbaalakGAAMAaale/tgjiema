import time as _time
import re as _re
import hashlib
import hmac as _hmac
import ipaddress as _ipaddr

from fastapi import FastAPI, Request, Depends, HTTPException, Query, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import asyncio
from pathlib import Path
import datetime

from database import get_users_col, get_file_records_col, get_decode_logs_col
from utils.monitor import metrics
from config import settings

app = FastAPI(title="TG解码器管理后台")
security = HTTPBasic()


# ─── 密码哈希支持（PBKDF2-HMAC-SHA256，无需新增依赖）──────────────
# 哈希格式: $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>
# 若 ADMIN_PASSWORD 以此前缀开头，按哈希校验；否则按明文常量时间比较（向后兼容）
_PBKDF2_PREFIX = "$pbkdf2-sha256$"
_PBKDF2_ITERATIONS = 200_000  # OWASP 2023 推荐 ≥ 600k，平衡部署机器性能取 200k


def _verify_password(plaintext: str, stored: str) -> bool:
    """校验密码。支持明文（向后兼容）和 PBKDF2 哈希格式。

    - 哈希格式: $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>
    - 明文格式: 直接常量时间比较
    """
    if not stored:
        return False
    if stored.startswith(_PBKDF2_PREFIX):
        try:
            parts = stored.split("$")
            # parts: ['', 'pbkdf2-sha256', '<iter>', '<salt_hex>', '<hash_hex>']
            if len(parts) != 5:
                return False
            iterations = int(parts[2])
            if iterations < 10_000:  # 防御：拒绝过低的迭代次数
                return False
            salt = bytes.fromhex(parts[3])
            expected_hash = bytes.fromhex(parts[4])
            # 对输入密码做同样的 PBKDF2 派生
            actual_hash = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), salt, iterations)
            return _hmac.compare_digest(actual_hash, expected_hash)
        except (ValueError, TypeError):
            return False
    # 明文模式：常量时间比较
    return secrets.compare_digest(plaintext.encode("utf-8"), stored.encode("utf-8"))


def generate_password_hash(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """生成 PBKDF2 哈希密码。可在外部脚本中调用以生成 .env 中的 ADMIN_PASSWORD 值。

    用法:
        python -c "from admin import generate_password_hash; print(generate_password_hash('YOUR_PASSWORD'))"
    """
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_PBKDF2_PREFIX}{iterations}${salt.hex()}${hash_bytes.hex()}"


# ─── 可信代理集合：仅这些来源的 X-Forwarded-For 才被信任 ─────────────
# 本地回环（IPv4/IPv6）+ Unix socket
_TRUSTED_PROXY_NETWORKS = (
    _ipaddr.ip_network("127.0.0.0/8"),
    _ipaddr.ip_network("::1/128"),
)


def _is_trusted_proxy(peer_host: str) -> bool:
    """判断直连对端是否为可信代理（本地回环）。"""
    if not peer_host:
        return False
    try:
        ip = _ipaddr.ip_address(peer_host)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXY_NETWORKS)


@app.on_event("startup")
async def startup():
    """启动时初始化数据库连接并从 SQLite 恢复 CSRF token 和登录失败计数。"""
    try:
        from database import init_db
        await init_db()
    except Exception as e:
        import sys
        from loguru import logger
        logger.error(f"[Admin] 数据库初始化失败，退出: {e}")
        try:
            from database import close_db
            await close_db()
        except Exception:
            pass
        sys.exit(1)
    await _load_state_from_cache()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ─── 登录速率限制 ──────────────────────────────────────────────
# IP -> [timestamps],记录 5 分钟内的失败时间戳
# 主存为进程内存（读写快），辅助持久化到 SQLite 恢复重启后状态
_login_failures: dict[str, list[float]] = {}
_LOGIN_LIMIT_WINDOW = settings.ADMIN_LOGIN_WINDOW
_LOGIN_LIMIT_MAX = settings.ADMIN_LOGIN_MAX_FAIL

# ─── CSRF 保护 ─────────────────────────────────────────────────
# 每个登录会话独立 token，防止跨站请求伪造
# 主存为进程内存，辅助持久化到 SQLite 恢复重启后状态
# value: (token, created_at_monotonic) — 加入过期时间，避免 token 永不过期导致：
#   1. 内存无限增长
#   2. 攻击者窃取 token 后可永久使用
_csrf_tokens: dict[str, tuple[str, float]] = {}
_CSRF_TOKEN_TTL: float = 3600.0  # 与 cookie max_age 对齐（1小时）

# M7: TTL 缓存 — 无筛选条件的 count_documents 走 CRDB 很贵，60s 缓存
# 仅缓存 {}, 有搜索条件时仍需 CRDB（regex 等无法缓存）
_count_cache: dict[str, tuple[float, int]] = {}
_COUNT_CACHE_TTL = settings.ADMIN_COUNT_CACHE_TTL
_SEARCH_MAX_LENGTH = settings.ADMIN_SEARCH_MAX_LENGTH
_background_tasks: set = set()


def _sanitize_search(raw: str) -> str:
    """S-6: 搜索输入消毒 —— 长度限制 + 正则特殊字符转义，防止 ReDoS 攻击。"""
    if not raw:
        return ""
    raw = raw.strip()[:_SEARCH_MAX_LENGTH]
    return _re.escape(raw)


async def _load_state_from_cache():
    """从 SQLite 恢复 CSRF token 和登录失败计数（进程重启后恢复）。"""
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        # 恢复 CSRF token
        csrf_data = await store.get("admin:csrf_tokens")
        if csrf_data:
            import json
            loaded = json.loads(csrf_data)
            # 兼容旧格式 (str) 和新格式 (tuple/list)
            now = _time.time()
            for k, v in loaded.items():
                if isinstance(v, str):
                    # 旧格式：纯字符串 token，给一个"已过期"的时间戳，
                    # 下次访问时会被 _get_csrf_token 重新生成
                    _csrf_tokens[k] = (v, now - _CSRF_TOKEN_TTL - 1)
                elif isinstance(v, (list, tuple)) and len(v) == 2:
                    try:
                        _csrf_tokens[k] = (str(v[0]), float(v[1]))
                    except (TypeError, ValueError):
                        continue
        # 恢复登录失败计数
        fail_data = await store.get("admin:login_failures")
        if fail_data:
            import json
            loaded = json.loads(fail_data)
            for ip, timestamps in loaded.items():
                _login_failures[ip] = timestamps
    except Exception as e:
        from loguru import logger
        logger.warning(f"[Admin] 从缓存恢复状态失败: {e}")


async def _persist_csrf_tokens():
    """异步持久化 CSRF token 到 SQLite（fire-and-forget）。

    仅持久化未过期的 token，避免重启后立即过期。
    """
    try:
        from database.cache_store import get_cache_store
        import json
        store = get_cache_store()
        now = _time.time()
        # 仅持久化未过期的 token，减小存储体积
        active = {
            u: [t, ts] for u, (t, ts) in _csrf_tokens.items()
            if now - ts < _CSRF_TOKEN_TTL
        }
        await store.set("admin:csrf_tokens", json.dumps(active))
    except Exception as e:
        from loguru import logger
        logger.warning(f"[Admin] 持久化CSRF token失败: {e}")


async def _persist_login_failures():
    """异步持久化登录失败计数到 SQLite（fire-and-forget）。"""
    try:
        from database.cache_store import get_cache_store
        import json
        store = get_cache_store()
        await store.set("admin:login_failures", json.dumps(_login_failures))
    except Exception as e:
        from loguru import logger
        logger.warning(f"[Admin] 持久化登录失败计数失败: {e}")


def _get_csrf_token(username: str = "") -> str:
    """获取或生成当前会话的 CSRF token。若 token 已过期则重新生成。"""
    if not username:
        return ""
    now = _time.time()
    entry = _csrf_tokens.get(username)
    if entry is None or (now - entry[1]) >= _CSRF_TOKEN_TTL:
        # 新生成或过期重新生成
        _csrf_tokens[username] = (secrets.token_hex(32), now)
        task = asyncio.ensure_future(_persist_csrf_tokens())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return _csrf_tokens[username][0]


def _verify_csrf(request: Request, form_token: str = None, username: str = "") -> bool:
    """验证 CSRF token:对比表单中的 csrf_token 和 cookie 中的 csrf_token。
    要求 cookie token 与当前登录用户的 token 一致，且与表单 token 一致。
    同时检查 token 是否已过期。"""
    cookie_token = request.cookies.get("csrf_token", "")
    if not cookie_token or not form_token:
        return False
    now = _time.time()
    # N-15-4: 按 username 精确绑定，而非 values() 全局匹配
    if username:
        entry = _csrf_tokens.get(username)
        if entry is None:
            return False
        expected_token, created_at = entry
        # 过期检查
        if (now - created_at) >= _CSRF_TOKEN_TTL:
            # 主动清理过期 token，避免内存泄漏
            _csrf_tokens.pop(username, None)
            return False
        return (secrets.compare_digest(cookie_token, expected_token)
                and secrets.compare_digest(cookie_token, form_token))
    # 无 username 时回退到全局匹配（兼容旧逻辑）—— 带过期检查
    for u, (t, ts) in list(_csrf_tokens.items()):
        if (now - ts) >= _CSRF_TOKEN_TTL:
            _csrf_tokens.pop(u, None)
            continue
        if secrets.compare_digest(cookie_token, t):
            return secrets.compare_digest(cookie_token, form_token)
    return False


def _get_client_ip(request: Request) -> str:
    """S-7: 解析真实客户端 IP。

    仅当直连对端为可信代理（本地回环）时才信任 X-Forwarded-For，
    否则使用直连对端 IP。防止客户端伪造 XFF 绕过登录速率限制。

    Nginx/Caddy 反向代理场景下，部署时应将 ADMIN_WEB_HOST 设为 127.0.0.1，
    此时直连对端为 127.0.0.1（可信代理），可正确解析 XFF 中的真实客户端。
    """
    if request is None:
        return "unknown"
    peer_host = request.client.host if request.client else ""
    # 仅可信代理场景才信任 XFF
    if peer_host and _is_trusted_proxy(peer_host):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # X-Forwarded-For 格式: "client, proxy1, proxy2"，取第一个（真实客户端）
            # 注意：可信代理设置的 XFF 中，最左侧是真实客户端 IP
            client_ip = forwarded.split(",")[0].strip()
            if client_ip:
                return client_ip
    return peer_host if peer_host else "unknown"


def verify_admin(credentials: HTTPBasicCredentials = Depends(security), request: Request = None):
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理员账号未配置,请在 .env 中设置 ADMIN_USERNAME 和 ADMIN_PASSWORD")

    # 速率限制检查
    client_ip = _get_client_ip(request)
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
        task = asyncio.ensure_future(_persist_login_failures())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    elif len(_login_failures[client_ip]) >= _LOGIN_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁,请 {_LOGIN_LIMIT_WINDOW // 60} 分钟后再试",
        )

    # 用户名常量时间比较
    correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        settings.ADMIN_USERNAME.encode("utf8"),
    )
    # 密码：支持 PBKDF2 哈希格式与明文（向后兼容）
    correct_password = _verify_password(credentials.password, settings.ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        # 记录失败
        _login_failures[client_ip].append(now)
        task = asyncio.ensure_future(_persist_login_failures())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        # RFC 7235: 401 必须带 WWW-Authenticate 头，提示浏览器弹出认证框
        raise HTTPException(
            status_code=401,
            detail="未授权访问",
            headers={"WWW-Authenticate": 'Basic realm="TG解码器管理后台", charset="UTF-8"'},
        )

    # 登录成功,清除该 IP 的失败记录
    _login_failures.pop(client_ip, None)
    task = asyncio.ensure_future(_persist_login_failures())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
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
        secure=settings.CSRF_COOKIE_SECURE,
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
    per_page = settings.ADMIN_PAGE_SIZE
    users_col = get_users_col()

    query = {}
    if search:
        search = _sanitize_search(search)
        if search.isdigit():
            query["user_id"] = int(search)
        else:
            query["$or"] = [
                {"username": {"$regex": search, "$options": "i"}},
                {"first_name": {"$regex": search, "$options": "i"}},
            ]

    # M7: 无筛选条件时走 60s TTL 缓存，避免每次翻页都 count_documents CRDB
    # 注意: count 和 find 之间数据可能变化，翻页时可能看到少量重复/遗漏记录
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
        username=admin,
    )


@app.post("/users/{user_id}/membership")
async def update_membership(
    user_id: int,
    request: Request,
    level: str = Form(...),
    csrf_token: str = Form(...),
    admin=Depends(verify_admin),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin):
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
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin), httponly=True, samesite="strict", secure=settings.CSRF_COOKIE_SECURE, max_age=3600)
    return response


@app.post("/users/{user_id}/toggle_ban")
async def toggle_ban(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    admin=Depends(verify_admin),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin):
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
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin), httponly=True, samesite="strict", secure=settings.CSRF_COOKIE_SECURE, max_age=3600)
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
        search = _sanitize_search(search)
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
        username=admin,
    )


@app.post("/files/{file_code}/delete")
async def delete_file(
    file_code: str,
    request: Request,
    csrf_token: str = Form(...),
    admin=Depends(verify_admin),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    files_col = get_file_records_col()
    result = await files_col.update_one(
        {"file_code": file_code},
        {"$set": {"status": "deleted"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="文件不存在")
    response = RedirectResponse(url="/files", status_code=303)
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin), httponly=True, samesite="strict", secure=settings.CSRF_COOKIE_SECURE, max_age=3600)
    return response


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    admin=Depends(verify_admin),
    page: int = Query(1, ge=1),
):
    per_page = settings.ADMIN_FILES_PAGE_SIZE
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
        username=admin,
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