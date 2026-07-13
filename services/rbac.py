"""R40 §9.2: RBAC — 基于角色的访问控制。

职责:
- 管理角色(rbac_roles): 超级管理员/安全管理员/运维/客服/运营
- 管理用户角色分配(rbac_user_roles)
- 权限检查: check_permission(user_id, permission)
- 初始化默认角色(幂等)

权限模型:
- super_admin 拥有所有权限(["*"])
- 其他角色拥有各自权限子集
- check_permission 先查用户角色,再查角色权限列表

数据表:
- rbac_roles: id/name/description/permissions(JSON)/created_at
- rbac_user_roles: user_id(PK)/role_id/assigned_at/assigned_by

设计要点:
- 纯函数式 + async
- 所有写入后调用 add_dirty_outbox() 确保跨机同步
- 权限列表存储为 JSON 数组字符串
"""
from __future__ import annotations

import datetime
import json
from loguru import logger

from database.cache_store import get_cache_store


# ─── 预定义角色 ────────────────────────────────────────────────
ROLE_SUPER_ADMIN = "super_admin"   # 超级管理员
ROLE_SECURITY = "security"         # 安全管理员
ROLE_OPS = "ops"                    # 运维
ROLE_SUPPORT = "support"            # 客服
ROLE_OPERATOR = "operator"         # 运营

# ─── 权限定义 ──────────────────────────────────────────────────
PERMISSION_VIEW_USERS = "users:view"
PERMISSION_BAN_USER = "users:ban"
PERMISSION_UNBAN_USER = "users:unban"
PERMISSION_VIEW_FILES = "files:view"
PERMISSION_DELETE_FILE = "files:delete"
PERMISSION_RESTORE_FILE = "files:restore"
PERMISSION_TAKEDOWN = "content:takedown"
PERMISSION_APPROVE_TAKEDOWN = "content:approve_takedown"
PERMISSION_VIEW_LOGS = "logs:view"
PERMISSION_VIEW_AUDIT = "audit:view"
PERMISSION_CONFIG_CHANGE = "config:change"
PERMISSION_BACKUP = "backup:manage"
PERMISSION_RESTORE = "backup:restore"
PERMISSION_MAINTENANCE = "system:maintenance"
PERMISSION_MANAGE_ROLES = "rbac:manage"

# R40 P0-8: CommandBus 高风险命令所需的细粒度权限(与 command_bus 中常量一致)
# 这些权限用于 RBAC check_permission 校验,必须出现在 rbac_roles 表的 permissions 中
PERMISSION_CONTENT_TAKEDOWN = "content:takedown"       # 内容下架(同 PERMISSION_TAKEDOWN)
PERMISSION_USERS_BAN = "users:ban"                     # 封禁用户(同 PERMISSION_BAN_USER)
PERMISSION_USERS_UNBAN = "users:unban"                 # 解封用户(同 PERMISSION_UNBAN_USER)
PERMISSION_RBAC_ASSIGN = "rbac:assign"                  # 分配角色(细粒度,不同于 rbac:manage)
PERMISSION_DISASTER_RESTORE = "disaster:restore"       # 灾备恢复
PERMISSION_MAINTENANCE_ENABLE = "maintenance:enable"   # 开启维护模式
PERMISSION_MAINTENANCE_DISABLE = "maintenance:disable" # 关闭维护模式
PERMISSION_DATA_PURGE = "data:purge"                   # 清除数据

# ─── 默认角色权限映射(模块级常量) ─────────────────────────────
# R40 P0-8: 新增 CommandBus 细粒度权限,分配给对应角色
_DEFAULT_ROLE_PERMISSIONS: dict[str, list[str]] = {
    ROLE_SUPER_ADMIN: ["*"],  # 所有权限
    ROLE_SECURITY: [
        PERMISSION_VIEW_USERS, PERMISSION_BAN_USER, PERMISSION_UNBAN_USER,
        PERMISSION_VIEW_FILES, PERMISSION_TAKEDOWN, PERMISSION_APPROVE_TAKEDOWN,
        PERMISSION_VIEW_LOGS, PERMISSION_VIEW_AUDIT,
        # R40 P0-8: 安全管理员可执行内容下架/封禁/分配角色
        PERMISSION_CONTENT_TAKEDOWN, PERMISSION_USERS_BAN, PERMISSION_USERS_UNBAN,
        PERMISSION_RBAC_ASSIGN,
    ],
    ROLE_OPS: [
        PERMISSION_VIEW_USERS, PERMISSION_VIEW_FILES, PERMISSION_VIEW_LOGS,
        PERMISSION_BACKUP, PERMISSION_RESTORE, PERMISSION_MAINTENANCE,
        # R40 P0-8: 运维可执行维护模式/灾备恢复/数据清除
        PERMISSION_MAINTENANCE_ENABLE, PERMISSION_MAINTENANCE_DISABLE,
        PERMISSION_DISASTER_RESTORE, PERMISSION_DATA_PURGE,
    ],
    ROLE_SUPPORT: [
        PERMISSION_VIEW_USERS, PERMISSION_VIEW_FILES, PERMISSION_VIEW_LOGS,
    ],
    ROLE_OPERATOR: [
        PERMISSION_VIEW_USERS, PERMISSION_BAN_USER, PERMISSION_VIEW_FILES,
        PERMISSION_DELETE_FILE,
        # R40 P0-8: 运营可执行封禁/解封
        PERMISSION_USERS_BAN, PERMISSION_USERS_UNBAN,
    ],
}


async def init_default_roles() -> int:
    """初始化默认角色(幂等),返回创建数量。

    使用 INSERT OR IGNORE 确保幂等: 已存在的角色不会被覆盖。

    Returns:
        新创建的角色数量(已存在的不计)
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[RBAC] init_default_roles 数据库未初始化")
        return 0

    created = 0
    now = datetime.datetime.now().isoformat()

    for role_name, permissions in _DEFAULT_ROLE_PERMISSIONS.items():
        try:
            # 查询角色是否已存在
            existing = await store._db.execute_fetchall(
                "SELECT id FROM rbac_roles WHERE name = ?",
                (role_name,),
            )
            if existing:
                continue

            desc = {
                ROLE_SUPER_ADMIN: "超级管理员,拥有所有权限",
                ROLE_SECURITY: "安全管理员,负责内容审核和用户封禁",
                ROLE_OPS: "运维,负责系统维护和备份",
                ROLE_SUPPORT: "客服,负责用户支持和查看",
                ROLE_OPERATOR: "运营,负责日常运营操作",
            }.get(role_name, "")

            # R40 P0-5: 业务表 + dirty_outbox 同事务
            async with store.transaction() as tx:
                await tx.execute(
                    "INSERT INTO rbac_roles (name, description, permissions, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (role_name, desc, json.dumps(permissions), now),
                )
                await store.add_dirty_outbox("rbac_roles", role_name, connection=tx)
                created += 1
        except Exception as e:
            logger.warning(f"[RBAC] init_default_roles 创建角色 {role_name} 失败: {e}")

    if created > 0:
        logger.info(f"[RBAC] init_default_roles 创建了 {created} 个默认角色")
    return created


async def create_role(name: str, permissions: list[str], description: str = "") -> int:
    """创建自定义角色。

    Args:
        name: 角色名(唯一)
        permissions: 权限列表
        description: 角色描述

    Returns:
        角色 ID(>0 表示成功); -1 表示失败(如已存在)
    """
    if not name:
        logger.warning("[RBAC] create_role 角色名不能为空")
        return -1

    store = get_cache_store()
    if not store._db:
        return -1

    now = datetime.datetime.now().isoformat()

    try:
        # 检查是否已存在
        existing = await store._db.execute_fetchall(
            "SELECT id FROM rbac_roles WHERE name = ?",
            (name,),
        )
        if existing:
            logger.warning(f"[RBAC] create_role 角色已存在: {name}")
            return -1

        # R40 P0-5: 业务表 + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                "INSERT INTO rbac_roles (name, description, permissions, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, description, json.dumps(permissions), now),
            )
            role_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if role_id > 0:
                await store.add_dirty_outbox("rbac_roles", str(role_id), connection=tx)
        logger.info(f"[RBAC] create_role 创建角色 {name} (id={role_id})")
        return role_id
    except Exception as e:
        logger.error(f"[RBAC] create_role 失败 name={name}: {e}")
        return -1


async def assign_role(user_id: int, role_name: str, assigned_by: int = 0) -> bool:
    """分配角色给用户。

    使用 INSERT OR REPLACE: 如果用户已有角色,则替换为新角色。
    (rbac_user_roles 表 user_id 为主键,一个用户只能有一个角色)

    Args:
        user_id: Telegram 用户 ID
        role_name: 角色名
        assigned_by: 分配人 ID(管理员)

    Returns:
        True 表示成功
    """
    store = get_cache_store()
    if not store._db:
        return False

    now = datetime.datetime.now().isoformat()

    try:
        # 查询角色 ID
        rows = await store._db.execute_fetchall(
            "SELECT id FROM rbac_roles WHERE name = ?",
            (role_name,),
        )
        if not rows:
            logger.warning(f"[RBAC] assign_role 角色不存在: {role_name}")
            return False

        role_id = int(rows[0][0])

        # R40 P0-5: 业务表 + audit_log + dirty_outbox 同事务
        async with store.transaction() as tx:
            await tx.execute(
                "INSERT OR REPLACE INTO rbac_user_roles (user_id, role_id, assigned_at, assigned_by) "
                "VALUES (?, ?, ?, ?)",
                (user_id, role_id, now, assigned_by),
            )
            await store.add_dirty_outbox("rbac_user_roles", str(user_id), connection=tx)

            # 写审计日志(同事务)
            await tx.execute(
                "INSERT INTO audit_log (actor_id, actor_type, action, target_type, target_id, details, created_at) "
                "VALUES (?, 'admin', 'assign_role', 'user', ?, ?, ?)",
                (
                    assigned_by,
                    str(user_id),
                    json.dumps({"role": role_name}),
                    now,
                ),
            )
            await store.add_dirty_outbox("audit_log", "last", connection=tx)

        logger.info(f"[RBAC] assign_role user={user_id} role={role_name} by={assigned_by}")
        return True
    except Exception as e:
        logger.error(f"[RBAC] assign_role 失败 user={user_id} role={role_name}: {e}")
        return False


async def revoke_role(user_id: int) -> bool:
    """撤销用户角色。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        True 表示成功(即使原本无角色也返回 True)
    """
    store = get_cache_store()
    if not store._db:
        return False

    try:
        # R40 P0-5: 业务表 + dirty_outbox 同事务
        async with store.transaction() as tx:
            await tx.execute(
                "DELETE FROM rbac_user_roles WHERE user_id = ?",
                (user_id,),
            )
            await store.add_dirty_outbox(
                "rbac_user_roles", str(user_id),
                operation="tombstone", connection=tx,
            )
        logger.info(f"[RBAC] revoke_role user={user_id} 角色已撤销")
        return True
    except Exception as e:
        logger.error(f"[RBAC] revoke_role 失败 user={user_id}: {e}")
        return False


async def get_user_role(user_id: int) -> str | None:
    """获取用户角色名。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        角色名;无角色返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None

    try:
        rows = await store._db.execute_fetchall(
            "SELECT r.name FROM rbac_user_roles ur "
            "JOIN rbac_roles r ON ur.role_id = r.id "
            "WHERE ur.user_id = ?",
            (user_id,),
        )
        if rows:
            return str(rows[0][0])
        return None
    except Exception as e:
        logger.warning(f"[RBAC] get_user_role 失败 user={user_id}: {e}")
        return None


async def check_permission(user_id: int, permission: str) -> bool:
    """检查用户是否有指定权限。

    R40 P0-8: fail-closed — 任何异常(DB 故障/连接错误等)一律返回 False,
    防止 RBAC 校验异常时意外放行高风险操作。

    判定逻辑:
    1. 获取用户角色
    2. 获取角色权限列表
    3. super_admin 拥有 ["*"] 通配权限,直接返回 True
    4. 检查 permission 是否在权限列表中

    Args:
        user_id: Telegram 用户 ID
        permission: 权限标识(如 "users:ban")

    Returns:
        True 表示有权限;False 表示无权限或校验异常(fail-closed)
    """
    try:
        role_name = await get_user_role(user_id)
        if role_name is None:
            return False

        permissions = await list_user_permissions(user_id)
        if not permissions:
            return False

        # super_admin 通配
        if "*" in permissions:
            return True

        return permission in permissions
    except Exception as e:
        # R40 P0-8: fail-closed — 异常时一律拒绝,防止 DB 故障导致越权
        logger.warning(
            f"[RBAC] check_permission 异常(fail-closed 拒绝) "
            f"user={user_id} perm={permission}: {e}"
        )
        return False


async def list_roles() -> list[dict]:
    """列出所有角色。

    Returns:
        角色列表 [{id, name, description, permissions, created_at}, ...]
    """
    store = get_cache_store()
    if not store._db:
        return []

    try:
        rows = await store._db.execute_fetchall(
            "SELECT id, name, description, permissions, created_at "
            "FROM rbac_roles ORDER BY id"
        )
        result = []
        for r in rows:
            perms_str = r[3] or "[]"
            try:
                perms = json.loads(perms_str) if isinstance(perms_str, str) else perms_str
            except (json.JSONDecodeError, TypeError):
                perms = []
            result.append({
                "id": r[0],
                "name": r[1],
                "description": r[2] or "",
                "permissions": perms,
                "created_at": r[4],
            })
        return result
    except Exception as e:
        logger.warning(f"[RBAC] list_roles 失败: {e}")
        return []


async def list_user_permissions(user_id: int) -> list[str]:
    """列出用户所有权限。

    R40 P0-8: fail-closed — 移除 _DEFAULT_ROLE_PERMISSIONS 回退,
    DB 不可用或查询异常时返回空列表(无权限),防止 DB 故障时
    凭借内存中的默认权限映射意外放行高风险操作。

    Args:
        user_id: Telegram 用户 ID

    Returns:
        权限列表;无角色或 DB 异常时返回空列表
    """
    role_name = await get_user_role(user_id)
    if role_name is None:
        return []

    store = get_cache_store()
    if not store._db:
        # R40 P0-8: fail-closed — DB 不可用时返回空列表(无权限)
        logger.warning(
            f"[RBAC] list_user_permissions DB 未初始化(fail-closed 返回空) "
            f"user={user_id} role={role_name}"
        )
        return []

    try:
        rows = await store._db.execute_fetchall(
            "SELECT r.permissions FROM rbac_user_roles ur "
            "JOIN rbac_roles r ON ur.role_id = r.id "
            "WHERE ur.user_id = ?",
            (user_id,),
        )
        if not rows:
            # R40 P0-8: fail-closed — DB 中无记录时返回空列表
            return []

        perms_str = rows[0][0] or "[]"
        try:
            perms = json.loads(perms_str) if isinstance(perms_str, str) else perms_str
        except (json.JSONDecodeError, TypeError):
            perms = []
        return perms
    except Exception as e:
        # R40 P0-8: fail-closed — 异常时返回空列表(无权限)
        logger.warning(
            f"[RBAC] list_user_permissions 异常(fail-closed 返回空) "
            f"user={user_id} role={role_name}: {e}"
        )
        return []


async def format_role_info(role: dict) -> str:
    """格式化角色信息为可读文本。

    Args:
        role: 角色字典(来自 list_roles 或 get_user_role)

    Returns:
        格式化的角色信息字符串
    """
    name = role.get("name", "未知")
    description = role.get("description", "")
    permissions = role.get("permissions", [])

    # 通配权限
    if "*" in permissions:
        perm_text = "所有权限(超级管理员)"
    else:
        perm_text = ", ".join(permissions) if permissions else "无"

    lines = [
        f"角色: {name}",
        f"描述: {description}" if description else "描述: 无",
        f"权限: {perm_text}",
    ]
    return "\n".join(lines)


# ─── R40 P0-8: CommandBus 支持函数 ─────────────────────────────


async def get_principal_permissions(principal_id: int) -> set[str]:
    """R40 P0-8: 获取 principal 的所有权限(返回 set,供 CommandBus 使用)。

    与 list_user_permissions 功能相同,但返回 set 类型,
    便于 CommandBus 进行权限集合操作。

    Args:
        principal_id: 操作者 ID(Telegram user_id 或 admin_id)

    Returns:
        权限集合;无角色或异常时返回空集合(fail-closed)
    """
    try:
        perms = await list_user_permissions(principal_id)
        return set(perms)
    except Exception as e:
        logger.warning(
            f"[RBAC] get_principal_permissions 异常(fail-closed 返回空集) "
            f"principal={principal_id}: {e}"
        )
        return set()


# ─── 所有权限定义(供 admin 页面展示) ─────────────────────────────

# 权限名称 → 中文描述
_PERMISSION_DESCRIPTIONS: dict[str, str] = {
    PERMISSION_VIEW_USERS: "查看用户列表",
    PERMISSION_BAN_USER: "封禁用户",
    PERMISSION_UNBAN_USER: "解封用户",
    PERMISSION_VIEW_FILES: "查看文件列表",
    PERMISSION_DELETE_FILE: "删除文件",
    PERMISSION_RESTORE_FILE: "恢复文件",
    PERMISSION_TAKEDOWN: "内容下架",
    PERMISSION_APPROVE_TAKEDOWN: "审批下架请求",
    PERMISSION_VIEW_LOGS: "查看日志",
    PERMISSION_VIEW_AUDIT: "查看审计日志",
    PERMISSION_CONFIG_CHANGE: "修改配置",
    PERMISSION_BACKUP: "备份管理",
    PERMISSION_RESTORE: "恢复备份",
    PERMISSION_MAINTENANCE: "系统维护",
    PERMISSION_MANAGE_ROLES: "角色管理",
    # R40 P0-8: CommandBus 细粒度权限
    PERMISSION_RBAC_ASSIGN: "分配角色(CommandBus)",
    PERMISSION_DISASTER_RESTORE: "灾备恢复(CommandBus)",
    PERMISSION_MAINTENANCE_ENABLE: "开启维护模式(CommandBus)",
    PERMISSION_MAINTENANCE_DISABLE: "关闭维护模式(CommandBus)",
    PERMISSION_DATA_PURGE: "清除数据(CommandBus)",
}


async def list_permissions() -> list[dict]:
    """列出所有已定义的权限。

    R40 P0-8: 供 admin RBAC 页面展示所有可用权限。

    Returns:
        权限列表 [{name, description}, ...]
    """
    return [
        {"name": name, "description": desc}
        for name, desc in _PERMISSION_DESCRIPTIONS.items()
    ]
