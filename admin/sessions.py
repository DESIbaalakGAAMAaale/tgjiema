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
from typing import Optional

from loguru import logger

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

    async def create_session(self, principal) -> str:
        """为登录的管理员创建 session。

        Args:
            principal: AdminPrincipal 对象(需有 id/username/roles 属性)

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
        try:
            return AdminPrincipal(
                id=int(data.get("principal_id", 0)),
                username=str(data.get("username", "")),
                roles=list(data.get("roles", [])),
            )
        except (TypeError, ValueError) as e:
            logger.warning(f"[admin.sessions] session 数据损坏: {e}")
            return None

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
