"""R40 P2-5: Admin Web 服务端会话管理。

职责:
    替代 HTTP Basic Auth 的无状态认证,改为服务端 session + Cookie 模式:
    1. create_session(principal) — 登录成功后创建 session_id 并存储主体信息
    2. validate_session(session_id) — 验证 session 有效性(存在且未过期)
    3. destroy_session(session_id) — 注销时主动销毁
    4. cleanup_expired_sessions() — 清理过期 session(由后台定时器调用)

设计原则:
    - session 数据持久化到 SQLite kv_store(跨进程共享,多 worker 兼容)
    - session_id 使用 secrets.token_urlsafe(32) 生成 256 位熵
    - session TTL 默认 8 小时(与 admin 后台使用场景匹配)
    - 携带 principal_id / username / roles / created_at / expires_at
    - 所有写入失败均降级返回(不让 admin 后台崩溃)
    - 中文注释,loguru 日志
"""
from __future__ import annotations

import datetime as _dt
import json
import secrets
import time
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    # R43: 仅用于类型注解,避免运行时循环导入(flake8 F821)
    from admin import AdminPrincipal

# session 在 kv_store 中的 key 前缀
_SESSION_KEY_PREFIX = "admin:session:"
# session 默认 TTL(秒)— 8 小时
_SESSION_TTL_SECONDS = 8 * 3600
# 清理时一次扫描的最大数量(避免阻塞 SQLite)
_CLEANUP_BATCH_SIZE = 200


def _now_iso() -> str:
    """当前 UTC 时间 ISO 字符串(秒精度)。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _make_session_key(session_id: str) -> str:
    """构造 kv_store 中的 session key。"""
    return f"{_SESSION_KEY_PREFIX}{session_id}"


async def _load_session_data(session_id: str) -> Optional[dict]:
    """从 kv_store 读取 session 数据,失败返回 None。"""
    if not session_id:
        return None
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            return None
        raw = await store.get_kv(_make_session_key(session_id))
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    except Exception as e:
        logger.debug(f"[admin.sessions] 读取 session 失败: {e}")
        return None


async def _save_session_data(session_id: str, data: dict) -> bool:
    """写入 session 数据到 kv_store,失败返回 False。"""
    if not session_id or not data:
        return False
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            return False
        await store.set_kv(
            _make_session_key(session_id),
            json.dumps(data, ensure_ascii=False, default=str),
        )
        return True
    except Exception as e:
        logger.debug(f"[admin.sessions] 写入 session 失败: {e}")
        return False


async def _delete_session_data(session_id: str) -> None:
    """从 kv_store 删除 session。"""
    if not session_id:
        return
    try:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            return
        # kv_store 没有专门的 delete_kv,用 set_kv 写空值再删除
        # 兼容性方案:写入过期标记,由 cleanup 清理
        # 更彻底的方式:直接执行 DELETE 语句
        await store._db.execute(
            "DELETE FROM kv_store WHERE key = ?",
            (_make_session_key(session_id),),
        )
        await store._db.commit()
    except Exception as e:
        logger.debug(f"[admin.sessions] 删除 session 失败: {e}")


class SessionManager:
    """R40 P2-5: 服务端会话管理器。

    用法:
        manager = SessionManager()
        # 登录成功后
        session_id = await manager.create_session(principal)
        # 后续请求
        principal = await manager.validate_session(session_id)
        if principal is None:
            # 重定向到 /login
        # 注销
        await manager.destroy_session(session_id)
    """

    def __init__(self, ttl_seconds: int = _SESSION_TTL_SECONDS):
        """初始化会话管理器。

        Args:
            ttl_seconds: session 有效期(秒),默认 8 小时
        """
        self.ttl_seconds = max(60, int(ttl_seconds))

    async def create_session(self, principal, mfa_verified: bool = True) -> str:
        """为登录的管理员创建 session。

        R41 P1-2: 新增 mfa_verified 参数,记录 MFA 验证状态。
        - MFA 未启用时:mfa_verified=True(无需 MFA)
        - MFA 已启用并完成验证:mfa_verified=True
        - MFA 已启用但未完成验证:不应调用此函数(应先走 /login/mfa)

        Args:
            principal: AdminPrincipal 对象(需有 id/username/roles 属性)
            mfa_verified: MFA 是否已验证(默认 True,兼容旧调用方)

        Returns:
            session_id(43 字符 URL-safe base64);失败返回空字符串
        """
        if principal is None:
            return ""
        # 生成 256 位熵的 session_id(secrets.token_urlsafe(32) 返回 43 字符)
        session_id = secrets.token_urlsafe(32)
        now = _now_iso()
        expires_at_ts = int(time.time()) + self.ttl_seconds
        expires_at_iso = (
            _dt.datetime.fromtimestamp(expires_at_ts, tz=_dt.timezone.utc)
            .isoformat(timespec="seconds")
        )
        data = {
            "session_id": session_id,
            "principal_id": int(getattr(principal, "id", 0)),
            "username": str(getattr(principal, "username", "")),
            "roles": list(getattr(principal, "roles", [])),
            "created_at": now,
            "expires_at": expires_at_iso,
            "expires_at_ts": expires_at_ts,
            # R41 P1-2: MFA 验证状态(MFA middleware 校验此字段)
            "mfa_verified": bool(mfa_verified),
        }
        ok = await _save_session_data(session_id, data)
        if not ok:
            logger.warning(f"[admin.sessions] 创建 session 失败(username={data['username']})")
            return ""
        logger.info(
            f"[admin.sessions] 创建 session user={data['username']} "
            f"ttl={self.ttl_seconds}s"
        )
        return session_id

    async def validate_session(self, session_id: str):
        """验证 session 有效性。

        R42 P0-3: 返回的 AdminPrincipal 从持久化身份表(admin_principals)读取,
        确保 session/RBAC/CommandBus 使用同一身份表读取的 ID 和角色,
        而非各自从 session 数据中生成不同的 ID。

        流程:
          1. 从 kv_store 读取 session 数据并校验过期时间
          2. 若 ADMIN_PRINCIPAL_ID 配置存在且 session.principal_id 匹配,
             从 admin_principals 表读取权威身份(含最新角色)
          3. 若配置不存在或持久化记录不存在,fallback 到 session 中的 principal_id/username/roles

        Args:
            session_id: 待验证的 session ID

        Returns:
            AdminPrincipal 对象(有效);None(无效或过期)
        """
        if not session_id:
            return None
        data = await _load_session_data(session_id)
        if data is None:
            return None
        # 检查过期时间
        expires_at_ts = data.get("expires_at_ts", 0)
        if not expires_at_ts or int(time.time()) >= int(expires_at_ts):
            # 主动清理已过期 session
            await _delete_session_data(session_id)
            logger.debug(f"[admin.sessions] session 已过期并清理: {session_id[:8]}...")
            return None
        # 延迟导入避免循环依赖
        from admin import AdminPrincipal
        session_principal_id = int(data.get("principal_id", 0) or 0)
        session_username = str(data.get("username", "") or "")
        session_roles = list(data.get("roles", []) or [])

        # R42 P0-3: 优先从持久化身份表读取 AdminPrincipal
        # 确保返回的 principal_id 与 RBAC/CommandBus 使用同一身份源
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if store._db and session_principal_id > 0:
                record = await store.get_admin_principal_record(session_principal_id)
                if record is not None and record.get("is_active", False):
                    # 从持久化记录构造 AdminPrincipal(使用 from_persistent_record 类方法)
                    # 角色从 admin_principal_roles 表读取(权威源)
                    persistent_roles = await store.list_admin_principal_roles(
                        session_principal_id
                    )
                    if persistent_roles:
                        record["roles"] = persistent_roles
                    return AdminPrincipal.from_persistent_record(record)
                # 持久化记录不存在或不活跃 → fallback 到 session 数据
                logger.debug(
                    f"[admin.sessions] 持久化记录不存在/不活跃 principal_id={session_principal_id},"
                    f"fallback 到 session 数据"
                )
        except Exception as e:
            logger.debug(f"[admin.sessions] 读取持久化身份失败,fallback 到 session 数据: {e}")

        # Fallback: 使用 session 中的 principal_id/username/roles
        try:
            return AdminPrincipal(
                id=session_principal_id,
                username=session_username,
                roles=session_roles,
            )
        except (TypeError, ValueError) as e:
            logger.warning(f"[admin.sessions] session 数据损坏: {e}")
            return None

    async def validate_or_raise(self, request) -> "AdminPrincipal":
        """R41 P1-1: 从 Request 中提取 session_id 并验证,失败时抛 HTTPException(401)。

        作为 async FastAPI 依赖使用:
            @app.get("/users")
            async def users_page(request: Request, admin=Depends(require_session)):
                ...

        与 validate_session 的区别:
        - validate_session 返回 None 表示无效(调用方需自行处理)
        - validate_or_raise 直接抛 HTTPException(401),适合作为 Depends 依赖

        Args:
            request: FastAPI Request 对象(从 cookie 读取 session_id)

        Returns:
            AdminPrincipal 对象(有效时)

        Raises:
            HTTPException: 401(无 session cookie / session 无效 / session 过期)
        """
        from fastapi import HTTPException
        session_id = ""
        if request is not None:
            session_id = request.cookies.get("session_id", "")
        if not session_id:
            raise HTTPException(
                status_code=401,
                detail="未登录或会话已过期",
                headers={"Location": "/login"},
            )
        principal = await self.validate_session(session_id)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="会话无效或已过期,请重新登录",
                headers={"Location": "/login"},
            )
        return principal

    async def destroy_session(self, session_id: str) -> None:
        """销毁 session(注销)。

        Args:
            session_id: 待销毁的 session ID
        """
        if not session_id:
            return
        await _delete_session_data(session_id)
        logger.info(f"[admin.sessions] 销毁 session: {session_id[:8]}...")

    async def cleanup_expired_sessions(self) -> int:
        """清理所有过期 session。

        扫描 kv_store 中 admin:session:* 前缀的所有 key,
        解析 expires_at_ts 字段,删除已过期的记录。

        Returns:
            清理的 session 数量
        """
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store._db:
                return 0
            # 查询所有 session key(LIKE 模式匹配)
            rows = await store._db.execute_fetchall(
                "SELECT key, value FROM kv_store WHERE key LIKE ? LIMIT ?",
                (f"{_SESSION_KEY_PREFIX}%", _CLEANUP_BATCH_SIZE),
            )
            if not rows:
                return 0
            now_ts = int(time.time())
            deleted = 0
            for key, value in rows:
                try:
                    data = json.loads(value) if value else {}
                    expires_at_ts = int(data.get("expires_at_ts", 0))
                    if expires_at_ts and now_ts >= expires_at_ts:
                        await store._db.execute(
                            "DELETE FROM kv_store WHERE key = ?", (key,),
                        )
                        deleted += 1
                except (json.JSONDecodeError, TypeError, ValueError):
                    # 损坏的 session 数据,直接删除
                    await store._db.execute(
                        "DELETE FROM kv_store WHERE key = ?", (key,),
                    )
                    deleted += 1
            if deleted > 0:
                await store._db.commit()
                logger.info(f"[admin.sessions] 清理过期 session: {deleted} 条")
            return deleted
        except Exception as e:
            logger.warning(f"[admin.sessions] 清理过期 session 失败: {e}")
            return 0


# 模块级单例(与 cache_store.get_cache_store() 模式一致)
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """获取 SessionManager 单例。"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
