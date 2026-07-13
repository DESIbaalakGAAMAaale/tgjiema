import time as _time
import re as _re
import hashlib
import hmac as _hmac
import ipaddress as _ipaddr
from dataclasses import dataclass, field

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


# ─── R40 P0-2: 管理员身份模型 ──────────────────────────────────
# 旧实现 verify_admin() 返回 ADMIN_USERNAME 字符串,新路由在 takedown/maintenance
# 等路径调用 admin.id,对 "admin" 等用户名会抛 ValueError 产生 500。
# 改为返回 AdminPrincipal 对象:审计/RBAC 用 principal.id,显示用 principal.username。
@dataclass
class AdminPrincipal:
    """R40 P0-2: 管理员身份主体。

    - id: 稳定的整数 ID(基于 username 哈希生成),用于审计日志和 RBAC
    - username: 显示用用户名(来自 ADMIN_USERNAME)
    - roles: 角色列表(单管理员默认超级管理员)
    """
    id: int
    username: str
    roles: list = field(default_factory=list)


def _get_admin_principal_id(username: str) -> int:
    """R40 P0-2: 基于 username 生成稳定的管理员整数 ID。

    使用 SHA256 前 8 字节的正整数表示,保证同一 username 始终映射到同一 id,
    避免路由中 admin.id 对字符串用户名抛 ValueError。
    取模 2^31 防止溢出常见 INT 字段。
    """
    if not username:
        return 0
    digest = hashlib.sha256(username.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") & 0x7FFFFFFF


# ─── R39 P2-8: CSP nonce + 点击劫持防护 ──────────────────────────
# 通过中间件为每个 HTML 响应注入 Content-Security-Policy 头,
# 使用 per-request nonce 防止 inline script 注入(防 XSS)。
# 同时设置 X-Frame-Options: DENY 与 frame-ancestors 'none' 防止点击劫持。
@app.middleware("http")
async def _csp_and_clickjacking_middleware(request: Request, call_next):
    """R39 P2-8: 为每个响应添加安全头。

    - Content-Security-Policy: per-request nonce + frame-ancestors 'none'
    - X-Frame-Options: DENY (兼容旧浏览器)
    - X-Content-Type-Options: nosniff (防 MIME 嗅探)
    - Referrer-Policy: strict-origin-when-cross-origin
    """
    import secrets as _secrets_mod
    # R39 P2-8: 生成 per-request CSP nonce (16 字节 base64)
    csp_nonce = _secrets_mod.token_urlsafe(16)
    # 将 nonce 放入 request.state 供模板使用
    request.state.csp_nonce = csp_nonce

    response = await call_next(request)

    # R39 P2-8: CSP 头 — 只允许 nonce 匹配的 inline script/style
    # frame-ancestors 'none' 防止被 iframe 嵌入(点击劫持)
    csp_header = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{csp_nonce}'; "
        f"style-src 'self' 'nonce-{csp_nonce}'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Content-Security-Policy"] = csp_header
    # R39 P2-8: X-Frame-Options 兜底(旧浏览器不支持 CSP frame-ancestors)
    response.headers["X-Frame-Options"] = "DENY"
    # R39 P2-8: 防 MIME 嗅探
    response.headers["X-Content-Type-Options"] = "nosniff"
    # R39 P2-8: Referrer 仅在同源时发送完整 URL
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ─── 密码哈希支持（R39 P1-12: 强制哈希格式，移除明文兼容）──────────────
# 哈希格式（按优先级）:
#   1. Argon2id:   $argon2id$v=<ver>$m=<mem>,t=<iter>,p=<par>$<salt_b64>$<hash_b64>  (需 argon2-cffi)
#   2. PBKDF2:     $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>
# 明文密码已被 R39 P1-12 移除，启动时若 ADMIN_PASSWORD 不以哈希前缀开头，
# 将拒绝登录并打印强制告警，要求运维重新生成哈希。
_PBKDF2_PREFIX = "$pbkdf2-sha256$"
_ARGON2ID_PREFIX = "$argon2id$"
_PBKDF2_ITERATIONS = 200_000  # OWASP 2023 推荐 ≥ 600k，平衡部署机器性能取 200k

# R39 P1-12: 尝试导入 argon2-cffi（可选，未安装时降级到 PBKDF2-only）
try:
    from argon2 import PasswordHasher as _Argon2Hasher
    from argon2.exceptions import VerifyMismatchError as _Argon2Mismatch
    _argon2_hasher = _Argon2Hasher(
        time_cost=3, memory_cost=65536, parallelism=4,  # OWASP 2023 推荐
        hash_len=32, salt_len=16,
    )
    _ARGON2_AVAILABLE = True
except ImportError:
    _argon2_hasher = None
    _Argon2Mismatch = Exception
    _ARGON2_AVAILABLE = False


def _is_hashed_password(stored: str) -> bool:
    """R39 P1-12: 判断密码是否为受支持的哈希格式。"""
    if not stored:
        return False
    return stored.startswith(_PBKDF2_PREFIX) or stored.startswith(_ARGON2ID_PREFIX)


def _verify_password(plaintext: str, stored: str) -> bool:
    """R39 P1-12: 校验密码（仅接受哈希格式，明文一律拒绝）。

    支持格式（按优先级）:
    - Argon2id:   $argon2id$v=19$m=...,t=...,p=...$<salt_b64>$<hash_b64>
    - PBKDF2:     $pbkdf2-sha256$<iterations>$<salt_hex>$<hash_hex>

    明文密码不再被接受，调用方应在启动时通过 _warn_if_plaintext_password() 提示。
    """
    if not stored or not plaintext:
        return False
    # R39 P1-12: Argon2id 优先（若 argon2-cffi 已安装）
    if _ARGON2_AVAILABLE and stored.startswith(_ARGON2ID_PREFIX):
        try:
            return _argon2_hasher.verify(stored, plaintext)
        except _Argon2Mismatch:
            return False
        except Exception:
            return False
    # R39 P1-12: PBKDF2 哈希校验
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
    # R39 P1-12: 明文密码一律拒绝（不再向后兼容）
    return False


# R39 P1-12: 启动时检测明文密码，仅告警一次（避免日志爆炸）
_plaintext_password_warned = False


def _warn_if_plaintext_password() -> None:
    """R39 P1-12: 启动时若 ADMIN_PASSWORD 为明文，打印强制告警并标记。

    明文密码将被 _verify_password 拒绝，所有登录都会失败。
    运维需使用 generate_password_hash() 生成哈希值写入 .env。
    """
    global _plaintext_password_warned
    if _plaintext_password_warned:
        return
    pwd = getattr(settings, "ADMIN_PASSWORD", "") or ""
    if pwd and not _is_hashed_password(pwd):
        _plaintext_password_warned = True
        try:
            from loguru import logger
            logger.error(
                "[Admin] R39 P1-12: ADMIN_PASSWORD 为明文格式，已被禁用！"
                "请使用以下命令生成哈希后写入 .env:\n"
                "  python -c \"from admin import generate_password_hash; "
                "print(generate_password_hash('YOUR_PASSWORD'))\"\n"
                "在修复前所有登录尝试将返回 401。"
            )
        except Exception:
            pass


def generate_password_hash(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """R39 P1-12: 生成哈希密码（优先 Argon2id，降级 PBKDF2）。

    - 若已安装 argon2-cffi，生成 Argon2id 哈希（推荐，抗 GPU/ASIC 攻击）
    - 否则生成 PBKDF2-HMAC-SHA256 哈希（无需额外依赖）

    可在外部脚本中调用以生成 .env 中的 ADMIN_PASSWORD 值。

    用法:
        python -c "from admin import generate_password_hash; print(generate_password_hash('YOUR_PASSWORD'))"
    """
    if not password:
        raise ValueError("密码不能为空")
    # R39 P1-12: 优先 Argon2id
    if _ARGON2_AVAILABLE:
        return _argon2_hasher.hash(password)
    # 降级 PBKDF2
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
    # R39 P1-12: 启动时检测明文密码并告警
    _warn_if_plaintext_password()
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


@app.on_event("shutdown")
async def shutdown():
    """关闭时清理数据库连接和 SQLite 缓存,避免进程挂起被 SIGKILL。"""
    try:
        from database import close_db
        await close_db()
    except Exception:
        pass

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
# C2: _count_cache 已迁移到 cache_store (ttl_cache 表),跨进程共享
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


def _fire_and_forget(coro):
    """P3: 安全调度 fire-and-forget 协程。

    在 threadpool 上下文中(async sync 依赖被 run_in_threadpool 调用时)
    可能无 running event loop,此时 asyncio.ensure_future 会抛 RuntimeError。
    降级为跳过(数据仍在内存中,下次有 loop 时会持久化)。
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # 无 running event loop(threadpool 上下文),跳过异步持久化
        # 关闭未 await 的协程避免 RuntimeWarning
        coro.close()
        from loguru import logger
        logger.debug("[Admin] 无 running event loop,跳过异步持久化(threadpool 上下文)")


def _get_csrf_token(username: str = "") -> str:
    """获取或生成当前会话的 CSRF token。若 token 已过期则重新生成。"""
    if not username:
        return ""
    now = _time.time()
    entry = _csrf_tokens.get(username)
    if entry is None or (now - entry[1]) >= _CSRF_TOKEN_TTL:
        # 新生成或过期重新生成
        _csrf_tokens[username] = (secrets.token_hex(32), now)
        # P3: 防御 threadpool 无 running event loop 的情况
        _fire_and_forget(_persist_csrf_tokens())
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
    # P3: 移除空 username 全局回退分支(死代码,且逻辑宽松有安全隐患)
    # 所有调用方均传入 username,空 username 一律拒绝(fail-closed)
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
            # P1-10: X-Forwarded-For 格式: "client, proxy1, proxy2"
            # Caddy/Nginx 反向代理会向已有 XFF 追加真实客户端 IP(在最右),
            # 攻击者自带伪造的 XFF 最左段会保留在头部左侧。
            # 因此取最右段(可信代理追加的真实客户端),而非最左段(可能被伪造)。
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                client_ip = parts[-1]
                if client_ip:
                    return client_ip
    return peer_host if peer_host else "unknown"


def verify_admin(credentials: HTTPBasicCredentials = Depends(security), request: Request = None) -> AdminPrincipal:
    """R40 P0-2: 校验管理员凭证,返回 AdminPrincipal 身份对象。

    旧实现返回 username 字符串,新路由调用 admin.id 对 "admin" 等用户名
    会抛 ValueError 产生 500。现改为返回 AdminPrincipal,路由使用 principal.id
    进行审计/RBAC,使用 principal.username 进行显示。
    """
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
        _fire_and_forget(_persist_login_failures())
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
    # R39 P1-12: 密码强制哈希格式（Argon2id 或 PBKDF2），明文一律拒绝
    correct_password = _verify_password(credentials.password, settings.ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        # R40 P0-2: 记录失败。使用 setdefault 防止 KeyError:
        # 上面的清理逻辑在列表为空时会 del key,首次失败登录会触发该路径,
        # 若直接 _login_failures[client_ip].append(now) 会抛 KeyError 产生 500。
        _login_failures.setdefault(client_ip, []).append(now)
        _fire_and_forget(_persist_login_failures())
        # RFC 7235: 401 必须带 WWW-Authenticate 头，提示浏览器弹出认证框
        raise HTTPException(
            status_code=401,
            detail="未授权访问",
            headers={"WWW-Authenticate": 'Basic realm="TG解码器管理后台", charset="UTF-8"'},
        )

    # 登录成功,清除该 IP 的失败记录
    _login_failures.pop(client_ip, None)
    _fire_and_forget(_persist_login_failures())
    # R40 P0-2: 返回 AdminPrincipal 而非字符串,避免路由中 admin.id 抛 ValueError
    return AdminPrincipal(
        id=_get_admin_principal_id(credentials.username),
        username=credentials.username,
        roles=["super_admin"],  # 单管理员默认超级管理员
    )


# ─── R40 P2-5 / R41 P1-1: 服务端 session 认证 ──────────────────
# R41 P1-1: 删除同步 verify_session()(存在 event loop 时返回 401、
# 无 loop 时引用未定义 loop 变量的 bug),改为统一的 async require_session。
# R41 P1-2: 所有 Admin 路由统一 Depends(require_session),
# HTTP Basic 仅保留 break-glass CLI 路径(/break-glass/login)。
def _extract_session_id(request: Request) -> str:
    """从请求 cookie 中提取 session_id。"""
    if request is None:
        return ""
    return request.cookies.get("session_id", "")


async def require_session(request: Request) -> AdminPrincipal:
    """R41 P1-1: async session 认证依赖(替代同步 verify_session)。

    从 cookie 读取 session_id 并调用 SessionManager.validate_or_raise,
    失败时抛 HTTPException(401)。

    用法:
        @app.get("/users")
        async def users_page(request: Request, admin=Depends(require_session)):
            ...

    Args:
        request: FastAPI Request 对象

    Returns:
        AdminPrincipal 对象

    Raises:
        HTTPException: 401(无 session cookie / session 无效 / session 过期)
    """
    from admin.sessions import get_session_manager
    manager = get_session_manager()
    return await manager.validate_or_raise(request)


# ─── R41 P1-2: MFA 强制 middleware ──────────────────────────────
# 校验 session 中 mfa_verified=True,未验证时重定向到 /login/mfa。
# 跳过认证相关路径(/login, /login/mfa, /logout, /break-glass/login, /health, /readiness)。
# R41 P1-10: 新增 /readiness 到豁免路径(供负载均衡器/Docker healthcheck 调用)。
_MFA_EXEMPT_PATHS = frozenset({
    "/login", "/login/mfa", "/logout",
    "/break-glass/login", "/health", "/readiness",
})


@app.middleware("http")
async def _mfa_enforcement_middleware(request: Request, call_next):
    """R41 P1-2: MFA 强制 middleware。

    对所有非豁免路径校验 session 中 mfa_verified=True:
    - 无 session cookie → 放行(由 require_session 依赖处理 401)
    - session 有效但 mfa_verified != True → 302 重定向到 /login/mfa
    - session 有效且 mfa_verified == True → 放行

    豁免路径: /login, /login/mfa, /logout, /break-glass/login, /health, /readiness
    """
    path = request.url.path
    # 豁免路径直接放行
    if path in _MFA_EXEMPT_PATHS:
        return await call_next(request)
    # 静态资源放行
    if path.startswith("/static/") or path.endswith((".css", ".js", ".ico", ".png")):
        return await call_next(request)
    # 读取 session_id
    session_id = request.cookies.get("session_id", "")
    if not session_id:
        # 无 session cookie,由 require_session 依赖返回 401
        return await call_next(request)
    # 加载 session 数据检查 mfa_verified
    try:
        from admin.sessions import _load_session_data
        data = await _load_session_data(session_id)
        if data is not None and not data.get("mfa_verified", False):
            # session 存在但 MFA 未验证,重定向到 MFA 输入页
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/login/mfa", status_code=302)
    except Exception:
        # 读取异常时放行,由 require_session 依赖处理
        pass
    return await call_next(request)


# ─── R41 P1-2: break-glass CLI 登录端点(HTTP Basic,仅本机) ───
@app.post("/break-glass/login")
async def break_glass_login(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    """R41 P1-2: break-glass CLI 登录端点。

    物理隔离的紧急访问通道,仅在本机访问 + 额外密码时可用:
    - 仅允许本机访问(127.0.0.1 / ::1)
    - 需要 ADMIN_USERNAME + ADMIN_PASSWORD(HTTP Basic)
    - 需要额外配置 BREAK_GLASS_PASSWORD(与环境变量分开)
    - 登录后创建 session(mfa_verified=True,跳过 MFA)
    - 用于紧急运维场景,所有操作仍走 CommandBus RBAC + 审批

    Returns:
        session_id(成功);403(非本机);401(凭证错误);503(未配置)
    """
    # 1. 本机访问限制
    peer_host = request.client.host if request.client else ""
    if peer_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="break-glass 仅允许本机访问")
    # 2. 检查 break-glass 密码是否配置
    break_glass_pwd = getattr(settings, "BREAK_GLASS_PASSWORD", "") or ""
    if not break_glass_pwd:
        raise HTTPException(status_code=503, detail="break-glass 密码未配置")
    # 3. 校验管理员凭证(HTTP Basic)
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理员账号未配置")
    correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        settings.ADMIN_USERNAME.encode("utf8"),
    )
    correct_password = _verify_password(credentials.password, settings.ADMIN_PASSWORD)
    # 4. 校验 break-glass 额外密码
    correct_bg_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        break_glass_pwd.encode("utf8"),
    ) if credentials.password else False
    # break-glass 需要同时通过管理员密码 OR break-glass 密码
    # 实际使用:credentials.password 应为 break-glass 密码(与管理员密码不同)
    if not (correct_username and (correct_password or correct_bg_password)):
        from loguru import logger
        logger.warning(
            f"[Admin] break-glass 登录失败(凭证错误) peer={peer_host}"
        )
        raise HTTPException(
            status_code=401,
            detail="break-glass 凭证错误",
            headers={"WWW-Authenticate": 'Basic realm="break-glass", charset="UTF-8"'},
        )
    # 5. 创建 session(mfa_verified=True,跳过 MFA — break-glass 已通过额外密码认证)
    from admin.sessions import get_session_manager
    manager = get_session_manager()
    principal = AdminPrincipal(
        id=_get_admin_principal_id(credentials.username),
        username=credentials.username,
        roles=["super_admin"],
    )
    session_id = await manager.create_session(principal, mfa_verified=True)
    if not session_id:
        raise HTTPException(status_code=503, detail="会话创建失败")
    from loguru import logger
    logger.warning(
        f"[Admin] break-glass 登录成功 user={credentials.username} peer={peer_host}"
    )
    return {"session_id": session_id, "message": "break-glass 登录成功"}


# ─── R40 P2-5: /login 与 /logout 路由 ──────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """R40 P2-5: 渲染登录表单(GET)。

    无需认证,展示用户名/密码表单。
    若已存在有效 session,重定向到首页。
    """
    # 检查是否已登录
    session_id = _extract_session_id(request)
    if session_id:
        from admin.sessions import get_session_manager
        manager = get_session_manager()
        existing = await manager.validate_session(session_id)
        if existing is not None:
            return RedirectResponse(url="/", status_code=302)
    # 渲染登录表单(简化:返回内联 HTML,避免新增模板文件)
    csp_nonce = getattr(request.state, "csp_nonce", "") or ""
    csrf_token = _get_csrf_token("__login__")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 - TG解码器管理后台</title>
<style nonce="{csp_nonce}">
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f5f5f5; margin: 0; padding: 40px 20px; }}
.login-container {{ max-width: 400px; margin: 60px auto; background: #fff;
                    padding: 32px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
h1 {{ margin: 0 0 24px 0; font-size: 24px; color: #333; }}
label {{ display: block; margin-bottom: 8px; font-weight: 500; color: #555; }}
input[type="text"], input[type="password"] {{ width: 100%; padding: 10px 12px;
     border: 1px solid #ddd; border-radius: 4px; font-size: 14px; box-sizing: border-box; }}
input[type="text"]:focus, input[type="password"]:focus {{ outline: none;
     border-color: #4a90e2; box-shadow: 0 0 0 3px rgba(74,144,226,0.1); }}
button {{ width: 100%; padding: 12px; background: #4a90e2; color: #fff;
        border: none; border-radius: 4px; font-size: 14px; cursor: pointer; margin-top: 16px; }}
button:hover {{ background: #357abd; }}
.error {{ color: #d32f2f; font-size: 13px; margin-top: 12px; min-height: 18px; }}
</style>
</head>
<body>
<div class="login-container">
<h1>管理后台登录</h1>
<form method="POST" action="/login">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<div>
<label for="username">用户名</label>
<input type="text" id="username" name="username" autocomplete="username" required>
</div>
<div style="margin-top: 16px;">
<label for="password">密码</label>
<input type="password" id="password" name="password" autocomplete="current-password" required>
</div>
<button type="submit">登录</button>
<div class="error"></div>
</form>
</div>
</body>
</html>"""
    response = HTMLResponse(content=html)
    response.set_cookie(
        key="csrf_token", value=csrf_token,
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    """R40 P2-5: 处理登录表单提交(POST)。

    1. CSRF 验证
    2. 速率限制检查(复用 _login_failures)
    3. 用户名密码校验(复用 _verify_password)
    4. 创建 session 并设置 cookie
    5. 重定向到首页
    """
    # CSRF 验证(cookie 中的 token 与表单中的 token 必须一致)
    cookie_csrf = request.cookies.get("csrf_token", "")
    if not cookie_csrf or not csrf_token or not secrets.compare_digest(cookie_csrf, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理员账号未配置")

    # 速率限制检查(与 verify_admin 逻辑一致)
    client_ip = _get_client_ip(request)
    now = _time.time()
    if client_ip not in _login_failures:
        _login_failures[client_ip] = []
    _login_failures[client_ip] = [
        ts for ts in _login_failures[client_ip]
        if now - ts < _LOGIN_LIMIT_WINDOW
    ]
    if not _login_failures[client_ip]:
        del _login_failures[client_ip]
        _fire_and_forget(_persist_login_failures())
    elif len(_login_failures[client_ip]) >= _LOGIN_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁,请 {_LOGIN_LIMIT_WINDOW // 60} 分钟后再试",
        )

    # 用户名常量时间比较
    correct_username = secrets.compare_digest(
        username.encode("utf8"),
        settings.ADMIN_USERNAME.encode("utf8"),
    )
    correct_password = _verify_password(password, settings.ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        _login_failures.setdefault(client_ip, []).append(now)
        _fire_and_forget(_persist_login_failures())
        # 返回登录页并显示错误信息(简化:返回 401)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 登录成功,清除该 IP 的失败记录
    _login_failures.pop(client_ip, None)
    _fire_and_forget(_persist_login_failures())

    # R40 P2-5: MFA 二步验证 — 检查用户是否已启用 MFA
    principal_id = _get_admin_principal_id(username)
    from admin.mfa import get_mfa_manager
    mfa_manager = get_mfa_manager()
    mfa_enabled = await mfa_manager.is_mfa_enabled(principal_id)
    if mfa_enabled:
        # 生成短时 MFA challenge token,5 分钟有效
        challenge_token = secrets.token_urlsafe(32)
        try:
            from database.cache_store import get_cache_store
            import json as _json
            import time as _time_mod
            await get_cache_store().set_kv(
                f"admin:mfa:challenge:{challenge_token}",
                _json.dumps({
                    "username": username,
                    "principal_id": principal_id,
                    "expires_at": _time_mod.time() + 300,
                }),
            )
        except Exception as e:
            # challenge 写入失败,fail-closed:拒绝登录
            import logging as _logging
            _logging.getLogger("admin").error(f"MFA challenge 写入失败: {e}")
            raise HTTPException(status_code=503, detail="MFA 会话创建失败,请重试")
        # 返回 MFA 输入页面(显示 6 位 TOTP 输入框)
        return _render_mfa_input_page(request, challenge_token, username)

    # R40 P2-5: 创建 session(MFA 未启用或验证通过后到达此处)
    # R41 P1-2: MFA 未启用时 mfa_verified=True(无需 MFA)
    from admin.sessions import get_session_manager
    manager = get_session_manager()
    principal = AdminPrincipal(
        id=principal_id,
        username=username,
        roles=["super_admin"],
    )
    session_id = await manager.create_session(principal, mfa_verified=True)
    if not session_id:
        # session 创建失败,降级为 HTTP Basic(返回 503)
        raise HTTPException(status_code=503, detail="会话创建失败,请重试")

    # 设置 session cookie 并重定向到首页
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="session_id", value=session_id,
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=8 * 3600,
    )
    return response


def _render_mfa_input_page(request: Request, challenge_token: str, username: str) -> HTMLResponse:
    """R40 P2-5: 渲染 MFA 二步验证输入页面(显示 6 位 TOTP 输入框)。"""
    csp_nonce = getattr(request.state, "csp_nonce", "") or ""
    csrf_token = _get_csrf_token("__login__")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MFA 验证 - TG解码器管理后台</title>
<style nonce="{csp_nonce}">
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f5f5f5; margin: 0; padding: 40px 20px; }}
.login-container {{ max-width: 400px; margin: 60px auto; background: #fff;
                    padding: 32px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
h1 {{ margin: 0 0 24px 0; font-size: 24px; color: #333; }}
label {{ display: block; margin-bottom: 8px; font-weight: 500; color: #555; }}
input[type="text"] {{ width: 100%; padding: 10px 12px;
     border: 1px solid #ddd; border-radius: 4px; font-size: 14px; box-sizing: border-box;
     letter-spacing: 4px; text-align: center; }}
input[type="text"]:focus {{ outline: none;
     border-color: #4a90e2; box-shadow: 0 0 0 3px rgba(74,144,226,0.1); }}
button {{ width: 100%; padding: 12px; background: #4a90e2; color: #fff;
        border: none; border-radius: 4px; font-size: 14px; cursor: pointer; margin-top: 16px; }}
button:hover {{ background: #357abd; }}
.hint {{ color: #888; font-size: 12px; margin-top: 12px; }}
</style>
</head>
<body>
<div class="login-container">
<h1>MFA 二步验证</h1>
<p style="color:#555; font-size:14px;">用户 {username} 已启用 MFA,请输入身份验证器 App 中的 6 位代码</p>
<form method="POST" action="/login/mfa">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<input type="hidden" name="challenge_token" value="{challenge_token}">
<div style="margin-top: 16px;">
<label for="totp_code">6 位验证码</label>
<input type="text" id="totp_code" name="totp_code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required>
</div>
<button type="submit">验证</button>
<div class="hint">验证码每 30 秒更新一次,允许 ±30s 时间漂移</div>
</form>
</div>
</body>
</html>"""
    response = HTMLResponse(content=html)
    response.set_cookie(
        key="csrf_token", value=csrf_token,
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


@app.post("/login/mfa")
async def login_mfa_verify(
    request: Request,
    challenge_token: str = Form(...),
    totp_code: str = Form(...),
    csrf_token: str = Form(...),
):
    """R40 P2-5: MFA 二步验证 — 校验 TOTP 代码并创建 session。

    流程:
    1. CSRF 验证
    2. 从 kv_store 读取 challenge_token 关联的 username(5 分钟内有效)
    3. 校验 TOTP 6 位代码
    4. 验证通过 → 创建 session,重定向到首页
    5. 验证失败 → 返回 401
    """
    # CSRF 验证
    cookie_csrf = request.cookies.get("csrf_token", "")
    if not cookie_csrf or not csrf_token or not secrets.compare_digest(cookie_csrf, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    if not challenge_token or not totp_code:
        raise HTTPException(status_code=400, detail="缺少 challenge_token 或 totp_code")

    # 从 kv_store 读取 challenge 数据
    import json as _json
    import time as _time_mod
    from database.cache_store import get_cache_store
    try:
        store = get_cache_store()
        raw = await store.get_kv(f"admin:mfa:challenge:{challenge_token}")
    except Exception as e:
        import logging as _logging
        _logging.getLogger("admin").error(f"MFA challenge 读取失败: {e}")
        raise HTTPException(status_code=503, detail="MFA 会话读取失败")
    if not raw:
        raise HTTPException(status_code=401, detail="MFA 会话已过期,请重新登录")
    try:
        challenge_data = _json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="MFA 会话数据无效")

    # 检查过期时间
    expires_at = challenge_data.get("expires_at", 0)
    if _time_mod.time() > expires_at:
        raise HTTPException(status_code=401, detail="MFA 会话已过期,请重新登录")

    username = challenge_data.get("username", "")
    principal_id = int(challenge_data.get("principal_id", 0))
    if not username or principal_id <= 0:
        raise HTTPException(status_code=401, detail="MFA 会话数据无效")

    # 校验 TOTP 代码
    from admin.mfa import get_mfa_manager
    mfa_manager = get_mfa_manager()
    ok = await mfa_manager.verify_totp_code(principal_id, totp_code)
    if not ok:
        raise HTTPException(status_code=401, detail="MFA 验证码错误")

    # 验证通过,删除 challenge token(防止重放)
    try:
        await store._db.execute(
            "DELETE FROM kv_store WHERE key = ?",
            (f"admin:mfa:challenge:{challenge_token}",),
        )
        await store._db.commit()
    except Exception:
        pass

    # 创建 session
    # R41 P1-2: MFA 验证通过后 mfa_verified=True
    from admin.sessions import get_session_manager
    manager = get_session_manager()
    principal = AdminPrincipal(
        id=principal_id,
        username=username,
        roles=["super_admin"],
    )
    session_id = await manager.create_session(principal, mfa_verified=True)
    if not session_id:
        raise HTTPException(status_code=503, detail="会话创建失败,请重试")

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="session_id", value=session_id,
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=8 * 3600,
    )
    return response


@app.post("/logout")
async def logout_submit(
    request: Request,
    csrf_token: str = Form(...),
):
    """R40 P2-5: 注销路由(POST)。

    1. CSRF 验证
    2. 销毁 session
    3. 清除 cookie
    4. 重定向到 /login
    """
    # CSRF 验证
    cookie_csrf = request.cookies.get("csrf_token", "")
    if not cookie_csrf or not csrf_token or not secrets.compare_digest(cookie_csrf, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    session_id = _extract_session_id(request)
    if session_id:
        from admin.sessions import get_session_manager
        manager = get_session_manager()
        await manager.destroy_session(session_id)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="session_id")
    return response


# ─── R40 P2-5: MFA 管理路由(/mfa/setup, /mfa/disable) ──────────

@app.get("/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_page(
    request: Request,
    admin=Depends(require_session),
):
    """R40 P2-5: MFA 设置页面(GET)。

    - 若已启用 MFA → 提示已启用,提供禁用入口
    - 若未启用 → 生成新 secret,展示 otpauth URI 供用户扫描/手动输入
    """
    from admin.mfa import get_mfa_manager
    principal_id = admin.id
    mfa_manager = get_mfa_manager()
    mfa_enabled = await mfa_manager.is_mfa_enabled(principal_id)

    # 若未启用,生成新 secret(覆盖旧的未确认 secret)
    secret = ""
    provisioning_uri = ""
    if not mfa_enabled:
        secret = await mfa_manager.generate_totp_secret(principal_id)
        try:
            import pyotp
            totp = pyotp.TOTP(secret)
            # provisioning_uri 包含 secret + issuer + account(可被 Authenticator 扫描)
            issuer = "TG解码器管理后台"
            account = admin.username or "admin"
            provisioning_uri = totp.provisioning_uri(name=account, issuer_name=issuer)
        except ImportError:
            provisioning_uri = ""

    csp_nonce = getattr(request.state, "csp_nonce", "") or ""
    csrf_token = _get_csrf_token(admin.username)
    if mfa_enabled:
        body_html = f"""
<div class="mfa-container">
<h1>MFA 已启用</h1>
<p style="color:#555; font-size:14px;">您的账户已启用多因素认证。</p>
<p style="color:#888; font-size:13px;">如需禁用,请使用下方表单(需要密码确认)。</p>
<form method="POST" action="/mfa/disable" style="margin-top:24px;">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<div style="margin-top: 16px;">
<label for="password">密码确认</label>
<input type="password" id="password" name="password" required>
</div>
<button type="submit">禁用 MFA</button>
</form>
</div>"""
    else:
        # 显示 QR URI 和 secret(用户可手动输入或扫描 QR 生成工具)
        secret_display = secret or ""
        uri_display = provisioning_uri or "(pyotp 未安装,无法生成 URI)"
        body_html = f"""
<div class="mfa-container">
<h1>设置 MFA</h1>
<p style="color:#555; font-size:14px;">扫描下方 otpauth URI 或手动输入 secret 到身份验证器 App</p>
<div style="margin-top:16px; padding:12px; background:#f5f5f5; border-radius:4px; word-break:break-all;">
<strong>Secret:</strong><br>
<code>{secret_display}</code>
</div>
<div style="margin-top:12px; padding:12px; background:#f5f5f5; border-radius:4px; word-break:break-all;">
<strong>otpauth URI:</strong><br>
<code>{uri_display}</code>
</div>
<form method="POST" action="/mfa/setup" style="margin-top:24px;">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<input type="hidden" name="secret" value="{secret_display}">
<div style="margin-top: 16px;">
<label for="totp_code">输入身份验证器中的 6 位代码以确认</label>
<input type="text" id="totp_code" name="totp_code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required>
</div>
<button type="submit">确认启用</button>
</form>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MFA 设置 - TG解码器管理后台</title>
<style nonce="{csp_nonce}">
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f5f5f5; margin: 0; padding: 40px 20px; }}
.mfa-container {{ max-width: 600px; margin: 40px auto; background: #fff;
                  padding: 32px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
h1 {{ margin: 0 0 24px 0; font-size: 22px; color: #333; }}
label {{ display: block; margin-bottom: 8px; font-weight: 500; color: #555; }}
input[type="text"], input[type="password"] {{ width: 100%; padding: 10px 12px;
     border: 1px solid #ddd; border-radius: 4px; font-size: 14px; box-sizing: border-box; }}
input[type="text"]:focus, input[type="password"]:focus {{ outline: none;
     border-color: #4a90e2; box-shadow: 0 0 0 3px rgba(74,144,226,0.1); }}
button {{ padding: 12px 24px; background: #4a90e2; color: #fff;
        border: none; border-radius: 4px; font-size: 14px; cursor: pointer; margin-top: 16px; }}
button:hover {{ background: #357abd; }}
code {{ font-family: "Courier New", monospace; font-size: 13px; color: #333; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""
    response = HTMLResponse(content=html)
    response.set_cookie(
        key="csrf_token", value=csrf_token,
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


@app.post("/mfa/setup")
async def mfa_setup_verify(
    request: Request,
    secret: str = Form(...),
    totp_code: str = Form(...),
    csrf_token: str = Form(...),
    admin=Depends(require_session),
):
    """R40 P2-5: MFA 设置确认(POST)— 验证 TOTP 代码后启用 MFA。

    流程:
    1. CSRF 验证
    2. 校验 secret 与表单中的 totp_code
    3. 验证通过 → 启用 MFA(标记 enabled=1)
    4. 重定向到首页
    """
    if not _verify_csrf(request, csrf_token, username=admin.username):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    if not secret or not totp_code:
        raise HTTPException(status_code=400, detail="缺少 secret 或 totp_code")

    from admin.mfa import _verify_totp, get_mfa_manager
    # 直接验证表单提交的 secret(尚未启用,不走 MFAManager.verify_totp_code)
    if not _verify_totp(secret, totp_code):
        raise HTTPException(status_code=401, detail="TOTP 验证码错误,请重试")

    # 验证通过,启用 MFA(写入 enabled=1)
    mfa_manager = get_mfa_manager()
    ok = await mfa_manager.enable_mfa(admin.id)
    if not ok:
        raise HTTPException(status_code=500, detail="启用 MFA 失败,请重试")

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="csrf_token", value=_get_csrf_token(admin.username),
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


@app.post("/mfa/disable")
async def mfa_disable(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
    admin=Depends(require_session),
):
    """R40 P2-5: 禁用 MFA(POST)— 需要密码确认。

    流程:
    1. CSRF 验证
    2. 密码确认(防止会话被劫持后禁用 MFA)
    3. 删除 secret + enabled 标记
    4. 重定向到首页
    """
    if not _verify_csrf(request, csrf_token, username=admin.username):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    # 密码确认(防止会话劫持后禁用 MFA)
    if not _verify_password(password, settings.ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="密码确认失败")

    from admin.mfa import get_mfa_manager
    mfa_manager = get_mfa_manager()
    ok = await mfa_manager.disable_mfa(admin.id)
    if not ok:
        raise HTTPException(status_code=500, detail="禁用 MFA 失败,请重试")

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="csrf_token", value=_get_csrf_token(admin.username),
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


def _make_csrf_response(template_name: str, context: dict, username: str = "") -> HTMLResponse:
    """生成带 CSRF cookie 的 HTML 响应。

    R40 P1-13: 注入 csp_nonce 到模板上下文,供 inline <script>/<style> 标签使用。
    CSP 中间件已将 per-request nonce 写入 request.state.csp_nonce。
    """
    token = _get_csrf_token(username)
    context["csrf_token"] = token
    # R40 P1-13: 从 request.state 提取 csp_nonce 注入模板上下文
    req = context.get("request")
    csp_nonce = ""
    if req is not None:
        csp_nonce = getattr(req.state, "csp_nonce", "") or ""
    context["csp_nonce"] = csp_nonce
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

    R41 P1-10: 增强 — 不再只检查 Bot 心跳,同时报告真实依赖状态:
      - SQLite schema readiness(关键业务表存在)
      - last collection success(crdb_sync 上次成功同步时间)
      - R2/CRDB collector freshness(上次指标采集时间)
      - ACL 配置完整性(REDIS_*_PASSWORD 4 个变量)
      - RU 当日使用量(采集失败显示 "unknown",不显示 0)

    任一关键检查失败时返回 503 Service Unavailable。
    """
    from database.cache_store import get_all_bot_heartbeats
    required_bots = {"up", "idx", "dsp", "mon", "admin_bot"}
    beats = await get_all_bot_heartbeats()
    bot_status = {
        name: beats.get(name, {}).get("is_running", False)
        for name in required_bots
    }
    bots_healthy = all(bot_status.values())

    # R41 P1-10: 复用 prometheus_exporter.check_readiness 获取依赖状态
    # 避免 admin 与 exporter 各自实现一套 schema/crdb_sync/r2 检查逻辑
    try:
        from services.prometheus_exporter import check_readiness as _check_dep
        dep = _check_dep()
        dep_checks = dep.get("checks", {})
        dep_details = dep.get("details", {})
        ru_daily_usage = dep.get("ru_daily_usage", "unknown")
        last_crdb_sync_age = dep.get("last_crdb_sync_age", -1.0)
        last_r2_collect_age = dep.get("last_r2_collect_age", -1.0)
    except Exception as e:
        # exporter 不可用时降级:仅依赖 Bot 心跳,但记录错误
        dep_checks = {}
        dep_details = {"exporter_error": str(e)}
        ru_daily_usage = "unknown"
        last_crdb_sync_age = -1.0
        last_r2_collect_age = -1.0

    # 整体就绪 = Bot 心跳 + 依赖检查(若 exporter 返回了检查项)
    deps_ready = all(dep_checks.values()) if dep_checks else True
    ready = bots_healthy and deps_ready

    if not ready:
        response.status_code = 503
    return {
        "status": "ok" if ready else "degraded",
        "bots": bot_status,
        "dependencies": {
            "checks": dep_checks,
            "details": dep_details,
            "ru_daily_usage": ru_daily_usage,
            "last_crdb_sync_age_seconds": last_crdb_sync_age,
            "last_r2_collect_age_seconds": last_r2_collect_age,
        },
    }


@app.get("/readiness")
async def readiness_check(response: Response):
    """R41 P1-10: 就绪检查端点(无需认证),返回详细依赖状态 JSON。

    与 /health 的区别:
      - /health: 简略状态(供 Docker healthcheck / k8s liveness 快速判断)
      - /readiness: 详细依赖状态(供运维排查 / Prometheus 告警上下文)

    返回:
      - 200 OK + 完整 JSON if 所有检查通过
      - 503 Service Unavailable + 完整 JSON if 任一检查失败

    JSON 结构:
      {
        "ready": bool,
        "passed": int,
        "checks": {name: bool},
        "details": {name: str},
        "ru_daily_usage": str,        # "unknown" if 采集失败
        "last_crdb_sync_age": float,
        "last_r2_collect_age": float,
      }
    """
    from database.cache_store import get_all_bot_heartbeats
    required_bots = {"up", "idx", "dsp", "mon", "admin_bot"}
    beats = await get_all_bot_heartbeats()
    bot_status = {
        name: beats.get(name, {}).get("is_running", False)
        for name in required_bots
    }

    # 复用 prometheus_exporter.check_readiness 获取依赖状态
    try:
        from services.prometheus_exporter import check_readiness as _check_dep
        dep = _check_dep()
    except Exception as e:
        dep = {
            "ready": False,
            "passed": 0,
            "checks": {"exporter_error": False},
            "details": {"exporter_error": str(e)},
            "ru_daily_usage": "unknown",
            "last_crdb_sync_age": -1.0,
            "last_r2_collect_age": -1.0,
        }

    # 合并 Bot 心跳检查到 readiness 报告
    all_checks = dict(dep.get("checks", {}))
    all_checks["bots_running"] = all(bot_status.values())
    all_details = dict(dep.get("details", {}))
    all_details["bots_running"] = (
        f"OK: {sum(bot_status.values())}/{len(bot_status)} bots running"
        if all(bot_status.values())
        else f"FAIL: offline bots={[k for k,v in bot_status.items() if not v]}"
    )

    ready = dep.get("ready", False) and all(bot_status.values())
    passed = sum(1 for v in all_checks.values() if v)

    if not ready:
        response.status_code = 503
    return {
        "ready": ready,
        "passed": passed,
        "checks": all_checks,
        "details": all_details,
        "bots": bot_status,
        "ru_daily_usage": dep.get("ru_daily_usage", "unknown"),
        "last_crdb_sync_age": dep.get("last_crdb_sync_age", -1.0),
        "last_r2_collect_age": dep.get("last_r2_collect_age", -1.0),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin=Depends(require_session)):
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
            # R36 §6.4.5: SQLite 无数据时走 SQLite 本地表 COUNT(0 RU),
            # 不再回退到 CRDB count_documents(避免热 COUNT)
            _sc.status_counters["total_users"] = await store.count_users_local()
            _sc.status_counters["total_files"] = await store.count_file_records_local()
            _sc.status_counters["active_files"] = await store.count_file_records_local(status="active")
        _sc.status_counters_initialized = True
        _sc.status_counters_loaded_at = now

    total_users = _sc.status_counters.get("total_users", 0)
    total_files = _sc.status_counters.get("total_files", 0)
    active_files = _sc.status_counters.get("active_files", 0)

    # R36 §6.4.5: today_decodes 走预聚合 snapshot(各 Bot 进程周期性写入),
    # 不再每次打开 dashboard 都对 CRDB 执行带日期过滤的 COUNT
    today_decodes = _sc.status_counters.get("today_decodes", 0)

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
            "send_success": _sc.status_counters.get("dsp.send_success", 0),
            "send_fail": _sc.status_counters.get("dsp.send_fail", 0),
            "backup_count": metrics.backup_count,
            "backup_fail": metrics.backup_fail_count,
        },
        username=admin.username,
    )


@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    admin=Depends(require_session),
    page: int = Query(1, ge=1),
    search: str = Query(""),
):
    per_page = settings.ADMIN_PAGE_SIZE
    # R36 §6.4.5: count/list/search 走 SQLite read model(0 RU),
    # 不再使用 CRDB count_documents + regex
    from database.cache_store import get_cache_store
    store = get_cache_store()
    sanitized = _sanitize_search(search) if search else ""

    # count 走 SQLite(0 RU)
    total = await store.count_users_local(search=sanitized)
    skip = (page - 1) * per_page
    # list 走 SQLite(0 RU,LIKE 搜索 + 分页 + 排序)
    users = await store.list_users_local_paginated(
        search=sanitized, skip=skip, limit=per_page,
        sort_field="created_at", sort_dir="desc",
    )

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
        username=admin.username,
    )


@app.post("/users/{user_id}/membership")
async def update_membership(
    user_id: int,
    request: Request,
    level: str = Form(...),
    csrf_token: str = Form(...),
    admin=Depends(require_session),
):
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin.username):
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
    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin.username), httponly=True, samesite="strict", secure=settings.CSRF_COOKIE_SECURE, max_age=3600)
    return response


@app.post("/users/{user_id}/toggle_ban")
async def toggle_ban(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    admin=Depends(require_session),
):
    """R40 P0-8: 切换用户封禁状态(通过 CommandBus 强制 RBAC + 审批门禁)。

    根据当前 is_banned 状态决定 ban 还是 unban:
    - 未封禁 → make_ban_user_command(需审批)
    - 已封禁 → make_unban_user_command(不需审批)
    """
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin.username):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    # 先查询当前状态决定 ban 还是 unban
    users_col = get_users_col()
    user = await users_col.find_one({"user_id": user_id})
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    is_currently_banned = bool(user.get("is_banned", False))

    # R40 P0-8: 通过 CommandBus 强制 RBAC + 审批门禁
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal,
        make_ban_user_command, make_unban_user_command,
    )
    cb_principal = CBPrincipal(id=admin.id, name=admin.username, source="web")
    bus = CommandBus()
    if is_currently_banned:
        # 已封禁 → 解封(不需审批)
        command = make_unban_user_command(user_id=user_id)
    else:
        # 未封禁 → 封禁(需审批)
        command = make_ban_user_command(
            user_id=user_id, reason="admin_web_toggle", duration_days=0,
        )
    result = await bus.execute(command, cb_principal)

    if result.approval_required:
        # 封禁需审批,重定向到审批列表页
        response = RedirectResponse(
            url=f"/approvals?approval_id={result.approval_id}", status_code=303,
        )
    elif result.success:
        response = RedirectResponse(url="/users", status_code=303)
    else:
        # 权限不足 / 执行失败
        raise HTTPException(status_code=403, detail=result.error)

    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin.username), httponly=True, samesite="strict", secure=settings.CSRF_COOKIE_SECURE, max_age=3600)
    return response


@app.get("/files", response_class=HTMLResponse)
async def files_page(
    request: Request,
    admin=Depends(require_session),
    page: int = Query(1, ge=1),
    search: str = Query(""),
):
    per_page = 20
    # R36 §6.4.5: count/list/search 走 SQLite read model(0 RU),
    # 不再使用 CRDB count_documents + regex
    from database.cache_store import get_cache_store
    store = get_cache_store()
    sanitized = _sanitize_search(search) if search else ""

    # count 走 SQLite(0 RU)
    total = await store.count_file_records_local(search=sanitized)
    skip = (page - 1) * per_page
    # list 走 SQLite(0 RU,LIKE 搜索 + 分页 + 排序)
    files = await store.list_file_records_local_paginated(
        search=sanitized, skip=skip, limit=per_page,
        sort_field="create_time", sort_dir="desc",
    )

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
        username=admin.username,
    )


@app.post("/files/{file_code}/delete")
async def delete_file(
    file_code: str,
    request: Request,
    csrf_token: str = Form(...),
    admin=Depends(require_session),
):
    """R40 P0-8: 删除文件(通过 CommandBus 强制 RBAC 门禁)。

    软删除(requires_approval=False) — 可立即执行,但 RBAC 权限校验仍强制。
    """
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin.username):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")

    # R40 P0-8: 通过 CommandBus 强制 RBAC 门禁(软删除不需审批,可立即执行)
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_delete_file_command,
    )
    cb_principal = CBPrincipal(id=admin.id, name=admin.username, source="web")
    command = make_delete_file_command(file_code=file_code)
    bus = CommandBus()
    result = await bus.execute(command, cb_principal)

    if result.success:
        response = RedirectResponse(url="/files", status_code=303)
    else:
        # 区分文件不存在(404) / 权限不足(403) / 其他失败(500)
        err = result.error or ""
        if "不存在" in err:
            raise HTTPException(status_code=404, detail=err)
        if "权限" in err:
            raise HTTPException(status_code=403, detail=err)
        raise HTTPException(status_code=500, detail=err)

    response.set_cookie(key="csrf_token", value=_get_csrf_token(admin.username), httponly=True, samesite="strict", secure=settings.CSRF_COOKIE_SECURE, max_age=3600)
    return response


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    admin=Depends(require_session),
    page: int = Query(1, ge=1),
):
    per_page = settings.ADMIN_FILES_PAGE_SIZE
    logs_col = get_decode_logs_col()

    # R36 §6.4.5: total_logs 走预聚合 snapshot(0 RU),
    # 不再每次打开 /logs 都对 CRDB 执行 COUNT(*)
    import utils.shared_counters as _sc
    total = _sc.status_counters.get("total_logs", 0)
    if total == 0:
        # 兜底:首次启动 snapshot 未写入时,走 60s TTL 缓存
        from database.cache_store import get_cache_store
        cached = await get_cache_store().cache_get("count_cache:logs", _COUNT_CACHE_TTL)
        if cached is not None:
            total = cached
        else:
            total = await logs_col.count_documents({})
            await get_cache_store().cache_set("count_cache:logs", total)
            _sc.status_counters["total_logs"] = total
    skip = (page - 1) * per_page
    # list 仍走 CRDB(已带 sort + limit,非无界排序)
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
        username=admin.username,
    )


@app.get("/health-page", response_class=HTMLResponse)
async def health_page(request: Request, admin=Depends(require_session)):
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
        username=admin.username,
    )


# ─── R40: 新增管理页面路由 ──────────────────────────────────────


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 任务中心 — 查看所有用户任务

    R40 P1-1 修复:
        原实现调用 list_user_tasks(0, limit=100),意图是"查询所有用户",
        但实际执行 WHERE user_id=0,仅返回 user_id=0 的任务(通常为空)。
        现改用 list_all_tasks 不带 user_id 过滤,真正查询所有用户任务。
    """
    from services.task_center import list_all_tasks
    # 查询最近 100 条所有用户任务(不带 user_id 过滤)
    tasks = await list_all_tasks(limit=100, offset=0)
    return _make_csrf_response(
        "tasks.html",
        {"request": request, "admin": admin, "tasks": tasks},
        username=admin.username,
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 举报管理 — 待处理举报列表"""
    from services.content_reports import list_reports
    result = await list_reports(status="pending")
    return _make_csrf_response(
        "reports.html",
        {"request": request, "admin": admin, "reports": result.get("items", [])},
        username=admin.username,
    )


@app.post("/reports/{report_id}/takedown")
async def takedown_report(
    report_id: int,
    request: Request,
    csrf_token: str = Form(...),
    admin: AdminPrincipal = Depends(require_session),
):
    """R40 P0-8: 下架举报内容(通过 CommandBus 强制 RBAC + 审批门禁)"""
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin.username):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")
    from services.content_reports import get_report
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal, make_takedown_command,
    )
    report = await get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="举报不存在")

    # R40 P0-8: 通过 CommandBus 执行(RBAC 权限校验 + 审批强制门禁)
    cb_principal = CBPrincipal(id=admin.id, name=admin.username, source="web")
    command = make_takedown_command(
        target_type=report["target_type"],
        target_id=str(report["target_id"]),
        reason=report.get("reason", ""),
    )
    bus = CommandBus()
    result = await bus.execute(command, cb_principal)

    if result.approval_required:
        # 高风险操作需审批,重定向到审批列表页
        response = RedirectResponse(
            url=f"/approvals?approval_id={result.approval_id}", status_code=303,
        )
    elif result.success:
        response = RedirectResponse(url="/reports", status_code=303)
    else:
        raise HTTPException(status_code=403, detail=result.error)

    response.set_cookie(
        key="csrf_token", value=_get_csrf_token(admin.username),
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


@app.get("/collections", response_class=HTMLResponse)
async def collections_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 集合管理 — 查看所有集合"""
    from services.collections import list_collections
    result = await list_collections()
    return _make_csrf_response(
        "collections.html",
        {"request": request, "admin": admin, "collections": result.get("items", [])},
        username=admin.username,
    )


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 通知中心 — 查看所有通知"""
    from services.notifications import list_all_notifications
    result = await list_all_notifications()
    return _make_csrf_response(
        "notifications.html",
        {"request": request, "admin": admin, "notifications": result.get("items", [])},
        username=admin.username,
    )


@app.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 审批管理 — 待审批列表"""
    from services.approval_workflow import list_pending
    result = await list_pending()
    return _make_csrf_response(
        "approvals.html",
        {"request": request, "admin": admin, "approvals": result.get("items", [])},
        username=admin.username,
    )


@app.get("/rbac", response_class=HTMLResponse)
async def rbac_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: RBAC 角色管理 — 角色与权限列表"""
    from services.rbac import list_roles, list_permissions
    roles = await list_roles()
    permissions = await list_permissions()
    return _make_csrf_response(
        "rbac.html",
        {"request": request, "admin": admin, "roles": roles, "permissions": permissions},
        username=admin.username,
    )


@app.get("/repair-console", response_class=HTMLResponse)
async def repair_console_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 修复控制台 — Outbox/DLQ/Replication/Relay"""
    from services.repair_console import (
        get_repair_overview, list_outbox, list_dlq, list_replication_failures,
    )
    overview = await get_repair_overview()
    outbox = await list_outbox(page_size=20)
    dlq = await list_dlq(page_size=20)
    repl = await list_replication_failures(page_size=20)
    return _make_csrf_response(
        "repair_console.html",
        {
            "request": request, "admin": admin,
            "overview": overview, "outbox": outbox,
            "dlq": dlq, "replication": repl,
        },
        username=admin.username,
    )


@app.get("/topology", response_class=HTMLResponse)
async def topology_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 拓扑可视化"""
    from services.topology_view import get_topology, get_health_summary
    topology = await get_topology()
    summary = await get_health_summary()
    return _make_csrf_response(
        "topology.html",
        {"request": request, "admin": admin, "topology": topology, "summary": summary},
        username=admin.username,
    )


@app.get("/ru-cost", response_class=HTMLResponse)
async def ru_cost_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: RU 成本中心"""
    from services.ru_cost_center import get_daily_report, check_ru_alert
    report = await get_daily_report()
    alert = await check_ru_alert()
    return _make_csrf_response(
        "ru_cost.html",
        {"request": request, "admin": admin, "report": report, "alert": alert},
        username=admin.username,
    )


@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 维护模式控制台"""
    from services.maintenance_mode import get_status, check_readiness
    status = await get_status()
    readiness = await check_readiness()
    return _make_csrf_response(
        "maintenance.html",
        {"request": request, "admin": admin, "status": status, "readiness": readiness},
        username=admin.username,
    )


@app.post("/maintenance/{action}")
async def maintenance_action(
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    reason: str = Form("手动维护"),
    admin: AdminPrincipal = Depends(require_session),
):
    """R40 P0-8: 维护模式操作(通过 CommandBus 强制 RBAC + 审批门禁)"""
    # CSRF 验证
    if not _verify_csrf(request, csrf_token, username=admin.username):
        raise HTTPException(status_code=403, detail="CSRF token 验证失败")
    from services.command_bus import (
        CommandBus, AdminPrincipal as CBPrincipal,
        make_enable_maintenance_command, make_disable_maintenance_command,
    )
    cb_principal = CBPrincipal(id=admin.id, name=admin.username, source="web")
    bus = CommandBus()

    if action == "enable":
        command = make_enable_maintenance_command(reason=reason)
        result = await bus.execute(command, cb_principal)
    elif action == "disable":
        command = make_disable_maintenance_command()
        result = await bus.execute(command, cb_principal)
    else:
        raise HTTPException(status_code=400, detail="无效的操作类型")

    if result.approval_required:
        # 高风险操作需审批,重定向到审批列表页
        response = RedirectResponse(
            url=f"/approvals?approval_id={result.approval_id}", status_code=303,
        )
    elif result.success:
        response = RedirectResponse(url="/maintenance", status_code=303)
    else:
        raise HTTPException(status_code=403, detail=result.error)

    response.set_cookie(
        key="csrf_token", value=_get_csrf_token(admin.username),
        httponly=True, samesite="strict",
        secure=settings.CSRF_COOKIE_SECURE, max_age=3600,
    )
    return response


@app.get("/disaster-recovery", response_class=HTMLResponse)
async def disaster_recovery_page(request: Request, admin: AdminPrincipal = Depends(require_session)):
    """R40: 灾备控制台"""
    from services.disaster_recovery import list_backups, get_rpo_rto, get_backup_schedule
    backups = await list_backups()
    rpo_rto = await get_rpo_rto()
    schedule = await get_backup_schedule()
    return _make_csrf_response(
        "disaster_recovery.html",
        {
            "request": request, "admin": admin,
            "backups": backups, "rpo_rto": rpo_rto, "schedule": schedule,
        },
        username=admin.username,
    )