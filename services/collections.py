"""R40 §9.1.3 + R45 第 16 节: 文件集合 — 合集码 + 批量下载 + 版本管理。

为用户提供文件合集功能,可批量管理和下载多个文件。
支持集合更新版本号、部分失效提示等。

R45 第 16 节 Collections 整改:
- 修改集合必须使用乐观锁(CAS):UPDATE ... WHERE version = ? AND id = ?
  冲突时返回 conflict 错误,由调用方决定重试或放弃
- 批量取件前逐项校验权限与副本状态:batch_retrieve 返回部分结果,
  不因个别项失败导致整体失败(部分失效处理)
- list_collections 支持 cursor 分页(基于 id 游标,避免 OFFSET 性能问题)

设计要点:
- create_collection 调用 code_generator.build_collection_code() 生成唯一集合码
  (复用现有 code_generator 模块)
- add_files / remove_files 用 INSERT OR IGNORE / DELETE 批量操作 + 更新 item_count + 调用 update_version
- get_collection 联合查询 collections + collection_items + file_records_local
  (检查文件是否失效: deleted_at / status / expire_time)
- 每次写入后调用 add_dirty_outbox(table_name, pk) 触发 CRDB 同步
- 通过 get_cache_store() 获取 CacheStore 单例
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.code_generator import build_collection_code
from services.error_codes import AppError, ErrorCodes


def _safe_json_loads(val) -> Any:
    """安全反序列化 JSON 字符串,失败返回 None。"""
    if val is None or val == "":
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _is_expired(expire_time) -> bool:
    """检查 expire_time 是否已过期(支持 ISO 字符串或时间戳)。"""
    if not expire_time:
        return False
    try:
        # 兼容 ISO 字符串
        if isinstance(expire_time, str):
            exp = _dt.datetime.fromisoformat(expire_time)
        else:
            # 兼容数字时间戳
            exp = _dt.datetime.fromtimestamp(float(expire_time))
        return _dt.datetime.now() >= exp
    except (ValueError, TypeError):
        return False


async def create_collection(name: str, owner_id: int, description: str = "") -> dict:
    """创建集合,生成唯一集合码。

    Args:
        name: 集合名称
        owner_id: 所有者用户 ID
        description: 集合描述(可选)

    Returns:
        {id, code, name, owner_id, description};失败返回 {}
    """
    store = get_cache_store()
    if not store._db:
        logger.warning("[collections] CacheStore 未初始化")
        return {}
    now = _dt.datetime.now().isoformat()
    # 复用 code_generator 生成集合码
    code = build_collection_code()
    try:
        # R40 P0-5: 业务表 + dirty_outbox 同事务
        async with store.transaction() as tx:
            cursor = await tx.execute(
                """INSERT INTO collections (name, code, owner_id, description, version,
                                             item_count, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, 0, 'active', ?, ?)""",
                (name, code, owner_id, description, now, now),
            )
            coll_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
            if coll_id:
                await store.add_dirty_outbox("collections", str(coll_id), connection=tx)
        if coll_id:
            logger.info(
                f"[collections] 创建集合 id={coll_id} code={code} owner={owner_id}"
            )
            return {
                "id": coll_id,
                "code": code,
                "name": name,
                "owner_id": owner_id,
                "description": description,
            }
        return {}
    except Exception as e:
        logger.warning(f"[collections] create_collection 失败: {e}")
        return {}


async def add_files(collection_id: int, file_codes: list[str]) -> int:
    """批量添加文件到集合,返回新增数量(去重)。

    Args:
        collection_id: 集合 ID
        file_codes: 文件码列表

    Returns:
        实际新增数量(已存在的跳过)
    """
    store = get_cache_store()
    if not store._db or not file_codes:
        return 0
    now = _dt.datetime.now().isoformat()
    added = 0
    try:
        # R40 P0-5: collection_items + collections + dirty_outbox + version 同事务
        async with store.transaction() as tx:
            # 使用 INSERT OR IGNORE 去重(collection_items 表有唯一索引更好,
            # 当前 schema 无唯一约束,先查再插避免重复)
            for code in file_codes:
                # 检查是否已存在
                chk = await tx.execute(
                    "SELECT id FROM collection_items WHERE collection_id = ? AND file_code = ?",
                    (collection_id, code),
                )
                existing = await chk.fetchone()
                if existing:
                    continue
                cursor = await tx.execute(
                    """INSERT INTO collection_items (collection_id, file_code, added_at)
                       VALUES (?, ?, ?)""",
                    (collection_id, code, now),
                )
                if cursor and cursor.rowcount > 0:
                    added += 1
            # 更新 item_count + updated_at
            if added > 0:
                await tx.execute(
                    """UPDATE collections
                       SET item_count = (
                            SELECT COUNT(*) FROM collection_items WHERE collection_id = ?
                           ),
                           updated_at = ?
                       WHERE id = ?""",
                    (collection_id, now, collection_id),
                )
                await store.add_dirty_outbox("collections", str(collection_id), connection=tx)
                # collection_items 变更通过 collections pk 同步(简化)
                await store.add_dirty_outbox("collection_items", str(collection_id), connection=tx)
                # 升级版本号(同事务内)
                await _update_version_in_tx(tx, store, collection_id, now)
        logger.info(
            f"[collections] 添加 {added}/{len(file_codes)} 文件到集合 {collection_id}"
        )
        return added
    except Exception as e:
        logger.warning(f"[collections] add_files 失败: {e}")
        return 0


async def remove_files(collection_id: int, file_codes: list[str]) -> int:
    """从集合移除文件,返回移除数量。

    Args:
        collection_id: 集合 ID
        file_codes: 文件码列表

    Returns:
        实际移除数量
    """
    store = get_cache_store()
    if not store._db or not file_codes:
        return 0
    now = _dt.datetime.now().isoformat()
    removed = 0
    try:
        # R40 P0-5: collection_items + collections + dirty_outbox + version 同事务
        async with store.transaction() as tx:
            for code in file_codes:
                cursor = await tx.execute(
                    """DELETE FROM collection_items
                       WHERE collection_id = ? AND file_code = ?""",
                    (collection_id, code),
                )
                if cursor and cursor.rowcount > 0:
                    removed += 1
            if removed > 0:
                # 更新 item_count + updated_at
                await tx.execute(
                    """UPDATE collections
                       SET item_count = (
                            SELECT COUNT(*) FROM collection_items WHERE collection_id = ?
                           ),
                           updated_at = ?
                       WHERE id = ?""",
                    (collection_id, now, collection_id),
                )
                await store.add_dirty_outbox("collections", str(collection_id), connection=tx)
                await store.add_dirty_outbox("collection_items", str(collection_id), connection=tx)
                # 升级版本号(同事务内)
                await _update_version_in_tx(tx, store, collection_id, now)
        logger.info(
            f"[collections] 从集合 {collection_id} 移除 {removed}/{len(file_codes)} 文件"
        )
        return removed
    except Exception as e:
        logger.warning(f"[collections] remove_files 失败: {e}")
        return 0


async def get_collection(code: str) -> dict | None:
    """按集合码获取集合 + 项目列表(含文件状态检查)。

    Args:
        code: 集合码

    Returns:
        集合字典 {id, name, code, owner_id, description, version,
                 item_count, status, created_at, updated_at, items: [...]}
        items 中每项含 {file_code, status, added_at};
        不存在返回 None
    """
    store = get_cache_store()
    if not store._db:
        return None
    try:
        cursor = await store._db.execute(
            """SELECT id, name, code, owner_id, description, version,
                      item_count, status, created_at, updated_at
               FROM collections WHERE code = ?""",
            (code,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        collection = {
            "id": row[0], "name": row[1], "code": row[2],
            "owner_id": row[3], "description": row[4],
            "version": int(row[5] or 1), "item_count": int(row[6] or 0),
            "status": row[7], "created_at": row[8], "updated_at": row[9],
            "items": [],
        }
        # 查集合项目 + 关联文件状态
        items_cursor = await store._db.execute(
            """SELECT ci.file_code, ci.added_at,
                      fr.status, fr.deleted_at, fr.expire_time
               FROM collection_items ci
               LEFT JOIN file_records_local fr ON ci.file_code = fr.file_code
               WHERE ci.collection_id = ?
               ORDER BY ci.added_at ASC""",
            (collection["id"],),
        )
        items_rows = await items_cursor.fetchall()
        items = []
        for r in items_rows:
            file_code = r[0]
            added_at = r[1]
            file_status = (r[2] or "missing").lower()
            deleted_at = r[3]
            expire_time = r[4]
            # 判断对外状态
            if deleted_at:
                external_status = "deleted"
            elif file_status == "deleted":
                external_status = "deleted"
            elif file_status == "expired" or _is_expired(expire_time):
                external_status = "expired"
            elif file_status == "missing":
                external_status = "deleted"
            elif file_status in ("active", "ready"):
                external_status = "active"
            else:
                external_status = file_status
            items.append({
                "file_code": file_code,
                "added_at": added_at,
                "status": external_status,
            })
        collection["items"] = items
        return collection
    except Exception as e:
        logger.warning(f"[collections] get_collection 失败: {e}")
        return None


async def list_collections(
    owner_id: int,
    page: int = 1,
    page_size: int = 10,
    cursor: int = 0,
) -> dict:
    """分页列出用户的集合(支持 R45 cursor 分页,向后兼容 page/page_size)。

    R45 第 16 节:cursor 分页基于 id 游标(避免 OFFSET 性能问题)。
    - cursor=0(默认):返回第一页,按 created_at DESC + id DESC 排序
    - cursor>0:返回 id < cursor 的下一页(向前翻页)
    - 当提供 cursor 时,page 参数被忽略

    Args:
        owner_id: 所有者用户 ID
        page: 页码(从 1 开始,cursor 模式下被忽略)
        page_size: 每页条数(1-100)
        cursor: 上一页最后一条 id(0=首页,>0=取该 id 之前的记录)

    Returns:
        {items, total, page, page_size, total_pages,
         next_cursor, has_more}
        - next_cursor: 下一页起始 id(用于继续翻页);无更多数据时为 0
        - has_more: 是否还有更多数据
    """
    store = get_cache_store()
    default = {
        "items": [], "total": 0,
        "page": page, "page_size": page_size, "total_pages": 0,
        "next_cursor": 0, "has_more": False,
    }
    if not store._db:
        return default
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    try:
        # 总数
        c_cursor = await store._db.execute(
            "SELECT COUNT(*) FROM collections WHERE owner_id = ?",
            (owner_id,),
        )
        c_row = await c_cursor.fetchone()
        total = int(c_row[0]) if c_row else 0
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0

        # R45: cursor 分页模式 — 多取 1 条用于判断 has_more
        # 注:created_at DESC 排序下,id < cursor 表示"更早创建"的记录
        if cursor and cursor > 0:
            fetch_size = page_size + 1
            q_cursor = await store._db.execute(
                """SELECT id, name, code, owner_id, description, version,
                          item_count, status, created_at, updated_at
                   FROM collections
                   WHERE owner_id = ? AND id < ?
                   ORDER BY id DESC LIMIT ?""",
                (owner_id, cursor, fetch_size),
            )
        else:
            # 首页或 page 模式
            fetch_size = page_size + 1 if cursor == 0 and page == 1 else page_size
            offset = (page - 1) * page_size if cursor == 0 else 0
            q_cursor = await store._db.execute(
                """SELECT id, name, code, owner_id, description, version,
                          item_count, status, created_at, updated_at
                   FROM collections WHERE owner_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (owner_id, fetch_size, offset),
            )
        rows = await q_cursor.fetchall()
        has_more = len(rows) > page_size
        # 截断到 page_size 条
        rows_page = rows[:page_size]
        items = [
            {
                "id": r[0], "name": r[1], "code": r[2], "owner_id": r[3],
                "description": r[4], "version": int(r[5] or 1),
                "item_count": int(r[6] or 0), "status": r[7],
                "created_at": r[8], "updated_at": r[9],
            }
            for r in rows_page
        ]
        # 计算 next_cursor:本页最后一条的 id
        next_cursor = items[-1]["id"] if items and has_more else 0
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
    except Exception as e:
        logger.warning(f"[collections] list_collections 失败: {e}")
        return default


async def _update_version_in_tx(tx, store, collection_id: int, now: str) -> int:
    """在给定事务内递增集合版本号(R40 P0-5 内部辅助函数)。

    不调用 commit(由外层 UnitOfWork 控制),失败抛异常让外层回滚。
    """
    cursor = await tx.execute(
        """UPDATE collections
           SET version = version + 1, updated_at = ?
           WHERE id = ?""",
        (now, collection_id),
    )
    if cursor and cursor.rowcount > 0:
        await store.add_dirty_outbox("collections", str(collection_id), connection=tx)
        # 查询新版本号(同事务内读取)
        v_cursor = await tx.execute(
            "SELECT version FROM collections WHERE id = ?",
            (collection_id,),
        )
        v_row = await v_cursor.fetchone()
        new_version = int(v_row[0]) if v_row else 0
        logger.info(
            f"[collections] 集合 {collection_id} 版本升级到 v{new_version}"
        )
        return new_version
    return 0


async def update_version(collection_id: int) -> int:
    """集合更新版本号(添加/删除文件后调用)。

    Args:
        collection_id: 集合 ID

    Returns:
        新版本号;失败返回 0
    """
    store = get_cache_store()
    if not store._db:
        return 0
    now = _dt.datetime.now().isoformat()
    try:
        # R40 P0-5: 业务表 + dirty_outbox 同事务
        async with store.transaction() as tx:
            new_version = await _update_version_in_tx(tx, store, collection_id, now)
        return new_version
    except Exception as e:
        logger.warning(f"[collections] update_version 失败: {e}")
        return 0


async def check_items_status(collection_id: int) -> dict:
    """检查集合内文件状态,返回失效文件列表。

    Args:
        collection_id: 集合 ID

    Returns:
        {total, active, expired, deleted, failed_items: [{file_code, status}]}
    """
    store = get_cache_store()
    default = {
        "total": 0, "active": 0, "expired": 0, "deleted": 0,
        "failed_items": [],
    }
    if not store._db:
        return default
    try:
        cursor = await store._db.execute(
            "SELECT file_code FROM collection_items WHERE collection_id = ?",
            (collection_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return default
        total = len(rows)
        active = 0
        expired = 0
        deleted = 0
        failed_items = []
        for r in rows:
            file_code = r[0]
            # 查 file_records_local 状态(不返回 channel_id 等内部信息)
            f_cursor = await store._db.execute(
                """SELECT status, deleted_at, expire_time
                   FROM file_records_local WHERE file_code = ?""",
                (file_code,),
            )
            f_row = await f_cursor.fetchone()
            if not f_row:
                # 文件记录不存在(可能被清理)
                failed_items.append({"file_code": file_code, "status": "deleted"})
                deleted += 1
                continue
            status = (f_row[0] or "active").lower()
            deleted_at = f_row[1]
            expire_time = f_row[2]
            if deleted_at or status == "deleted":
                failed_items.append({"file_code": file_code, "status": "deleted"})
                deleted += 1
            elif status == "expired" or _is_expired(expire_time):
                failed_items.append({"file_code": file_code, "status": "expired"})
                expired += 1
            elif status in ("active", "ready"):
                active += 1
            else:
                # 其他异常状态(如 corrupted / channel_lost)
                failed_items.append({"file_code": file_code, "status": status})
                expired += 1
        return {
            "total": total,
            "active": active,
            "expired": expired,
            "deleted": deleted,
            "failed_items": failed_items,
        }
    except Exception as e:
        logger.warning(f"[collections] check_items_status 失败: {e}")
        return default


async def format_collection_info(collection: dict) -> str:
    """格式化集合信息为用户可读文本。

    Args:
        collection: get_collection / list_collections 返回的集合字典

    Returns:
        多行纯文本(避免 Telegram markdown 解析问题)
    """
    if not collection:
        return "集合不存在"
    lines = [
        f"📦 集合: {collection.get('name', '')}",
        f"集合码: {collection.get('code', '')}",
        f"文件数: {collection.get('item_count', 0)}",
        f"版本: v{collection.get('version', 1)}",
        f"状态: {collection.get('status', 'active')}",
    ]
    desc = collection.get("description", "")
    if desc:
        lines.append(f"描述: {desc}")
    # 文件列表(若有)
    items = collection.get("items") or []
    if items:
        lines.append("")
        lines.append("文件列表:")
        # 只展示前 10 个,避免消息过长
        for item in items[:10]:
            if isinstance(item, dict):
                file_code = item.get("file_code", "")
                status = item.get("status", "")
            else:
                file_code = str(item)
                status = ""
            status_tag = f" [{status}]" if status and status != "active" else ""
            lines.append(f"  - {file_code}{status_tag}")
        if len(items) > 10:
            lines.append(f"  ... 还有 {len(items) - 10} 个文件")
    return "\n".join(lines)


# R41 P1-12: 集合解析与权限校验
# 用于用户访问集合时(批量下载/分享),验证对集合中所有文件的访问权限,
# 并返回文件码列表 + 元数据(供 dsp_bot 派送)。


async def resolve_collection(user_id: int, collection_id: int) -> dict:
    """R41 P1-12: 解析集合并校验用户对集合中所有文件的访问权限。

    权限规则:
        - 集合 owner_id == user_id → 拥有所有文件的访问权限
        - 其他用户 → 校验每个文件的 file_record.uploader_id:
            * uploader_id == user_id → 允许
            * uploader_id != user_id → 拒绝(返回 allowed=False)
        - 任一文件被下架/删除 → 跳过该文件,但不阻塞整个集合解析

    Args:
        user_id: 当前访问用户 ID
        collection_id: 集合 ID

    Returns:
        {
            "allowed": bool,           # 是否允许访问(权限校验)
            "collection_id": int,      # 集合 ID
            "collection_code": str,    # 集合码
            "collection_name": str,    # 集合名称
            "owner_id": int,           # 集合所有者 ID
            "items": [                 # 文件项列表
                {
                    "file_code": str,
                    "added_at": str,
                    "status": str,         # active / deleted / expired
                    "uploader_id": int,    # 文件上传者 ID
                    "has_access": bool,    # 当前用户是否有访问权限
                },
                ...
            ],
            "denied_items": [          # 无权限的文件码列表
                {"file_code": str, "uploader_id": int},
                ...
            ],
            "error": str,              # 错误描述(集合不存在/数据库未就绪)
        }
    """
    store = get_cache_store()
    if not store._db:
        return {
            "allowed": False, "collection_id": collection_id,
            "collection_code": "", "collection_name": "",
            "owner_id": 0, "items": [], "denied_items": [],
            "error": "数据库未初始化",
        }
    try:
        # 查询集合信息(校验所有权)
        cursor = await store._db.execute(
            """SELECT id, name, code, owner_id, status, item_count
               FROM collections WHERE id = ?""",
            (collection_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {
                "allowed": False, "collection_id": collection_id,
                "collection_code": "", "collection_name": "",
                "owner_id": 0, "items": [], "denied_items": [],
                "error": "集合不存在",
            }
        coll_id = int(row[0])
        coll_name = row[1] or ""
        coll_code = row[2] or ""
        owner_id = int(row[3]) if row[3] else 0
        coll_status = row[4] or "active"
        # 查询集合项 + 关联文件的 uploader_id
        items_cursor = await store._db.execute(
            """SELECT ci.file_code, ci.added_at,
                      fr.uploader_id, fr.status, fr.deleted_at, fr.expire_time
               FROM collection_items ci
               LEFT JOIN file_records_local fr ON ci.file_code = fr.file_code
               WHERE ci.collection_id = ?
               ORDER BY ci.added_at ASC""",
            (coll_id,),
        )
        items_rows = await items_cursor.fetchall()
        items: list[dict] = []
        denied_items: list[dict] = []
        for r in items_rows:
            file_code = r[0] or ""
            added_at = r[1] or ""
            uploader_id = int(r[2]) if r[2] else 0
            file_status = (r[3] or "missing").lower()
            deleted_at = r[4]
            expire_time = r[5]
            # 判断文件状态(对外可见性)
            if deleted_at or file_status == "deleted":
                external_status = "deleted"
            elif file_status == "expired" or _is_expired(expire_time):
                external_status = "expired"
            elif file_status in ("active", "ready"):
                external_status = "active"
            else:
                external_status = "deleted" if file_status == "missing" else file_status
            # 权限校验:owner 直通;其他用户校验 uploader_id
            if owner_id == user_id:
                has_access = True
            elif uploader_id == user_id and uploader_id != 0:
                has_access = True
            else:
                has_access = False
                denied_items.append({
                    "file_code": file_code,
                    "uploader_id": uploader_id,
                })
            items.append({
                "file_code": file_code,
                "added_at": added_at,
                "status": external_status,
                "uploader_id": uploader_id,
                "has_access": has_access,
            })
        # 整体权限:owner 或所有项都有访问权限
        allowed = (owner_id == user_id) or (
            len(denied_items) == 0 and len(items) > 0
        )
        # 集合本身已被软删除/禁用 → 拒绝访问
        if coll_status != "active":
            allowed = False
        return {
            "allowed": allowed,
            "collection_id": coll_id,
            "collection_code": coll_code,
            "collection_name": coll_name,
            "owner_id": owner_id,
            "items": items,
            "denied_items": denied_items,
            "error": "" if allowed else "无访问权限" if coll_status == "active" else "集合已禁用",
        }
    except Exception as e:
        logger.warning(f"[collections] resolve_collection 失败: {e}")
        return {
            "allowed": False, "collection_id": collection_id,
            "collection_code": "", "collection_name": "",
            "owner_id": 0, "items": [], "denied_items": [],
            "error": f"解析失败: {e}",
        }


# ─── R45 第 16 节: 集合乐观锁(CAS)+ 批量取件 ───────────────────


async def update_collection(
    collection_id: int,
    name: str | None = None,
    description: str | None = None,
    expected_version: int | None = None,
    bypass_cas: bool = False,
) -> dict:
    """R45 第 16 节 + R51 P1-3: 修改集合元数据(乐观锁 CAS,强制 expected_version)。

    R51 P1-3 整改要点:
    - 移除 ``expected_version=None → 跳过 CAS`` 的旧兼容路径(默认拒绝)
    - 生产路径必须显式提供 ``expected_version``(否则 raise AppError)
    - 仅运维/迁移场景显式传 ``bypass_cas=True`` 才能跳过 CAS 校验
    - CAS 冲突从"返回 conflict=True"改为 raise AppError(COLLECTION_CAS_CONFLICT)

    使用 ``WHERE version = ? AND id = ?`` 进行 CAS 更新:
        - expected_version 匹配成功 → 更新 + 版本号 +1,返回 success
        - expected_version 不匹配(被其他写入抢占)→ raise AppError(COLLECTION_CAS_CONFLICT)
        - bypass_cas=True 且 expected_version=None → 跳过 CAS(仅限运维/迁移)

    并发安全:
        - 单个 UPDATE 语句原子完成 CAS 检查 + 版本递增
        - 失败时不写 dirty_outbox(避免无变更触发同步)
        - 调用方收到 AppError(COLLECTION_CAS_CONFLICT)时应重新读取最新版本后重试

    Args:
        collection_id: 集合 ID
        name: 新名称(None=不修改)
        description: 新描述(None=不修改)
        expected_version: 调用方读取时的版本号(乐观锁校验)
            生产路径必填(>0),None 时必须同时传 bypass_cas=True
        bypass_cas: 是否显式跳过 CAS(仅运维/迁移使用,默认 False)

    Returns:
        {success: bool, conflict: bool, new_version: int,
         current_version: int, message: str}
        - success=True & conflict=False:更新成功

    Raises:
        AppError(COLLECTION_CAS_VERSION_REQUIRED): 未传 expected_version 且未 bypass_cas
        AppError(COLLECTION_CAS_BYPASS_NOT_ALLOWED): expected_version 与 bypass_cas 同时设置
        AppError(COLLECTION_CAS_CONFLICT): 版本冲突(expected_version 不匹配)
    """
    store = get_cache_store()
    if not store._db:
        raise AppError(
            ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
            params={"collection_id": collection_id, "reason": "cache_store_unavailable"},
        )
    # R51 P1-3: bypass_cas 与 expected_version 不可同时设置(避免歧义)
    if bypass_cas and expected_version is not None and int(expected_version) > 0:
        raise AppError(
            ErrorCodes.COLLECTION_CAS_BYPASS_NOT_ALLOWED,
            params={
                "collection_id": collection_id,
                "reason": "bypass_cas_and_expected_version_mutually_exclusive",
            },
        )
    # R51 P1-3: 生产路径必须传 expected_version(否则显式 bypass_cas=True)
    if (expected_version is None or int(expected_version) <= 0) and not bypass_cas:
        raise AppError(
            ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
            params={
                "collection_id": collection_id,
                "reason": "expected_version_required_or_bypass_cas_explicit",
            },
        )
    now = _dt.datetime.now().isoformat()

    # 构造 SET 子句(只更新非 None 字段)
    set_clauses: list[str] = []
    params: list[Any] = []
    if name is not None:
        set_clauses.append("name = ?")
        params.append(name)
    if description is not None:
        set_clauses.append("description = ?")
        params.append(description)
    if not set_clauses:
        # 无字段需更新 — 直接读取当前版本
        try:
            cur = await store._db.execute(
                "SELECT version FROM collections WHERE id = ?",
                (collection_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise AppError(
                    ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
                    params={
                        "collection_id": collection_id,
                        "reason": "collection_not_found",
                    },
                )
            return {
                "success": True, "conflict": False,
                "new_version": int(row[0] or 1),
                "current_version": int(row[0] or 1),
                "message": "无字段需更新",
            }
        except AppError:
            raise
        except Exception as e:
            raise AppError(
                ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
                params={
                    "collection_id": collection_id,
                    "reason": f"query_version_failed: {e}",
                },
            )

    # 加入 version 递增 + updated_at
    set_clauses.append("version = version + 1")
    set_clauses.append("updated_at = ?")
    params.append(now)

    # WHERE 条件:必含 id = ?;若提供 expected_version 则加 version = ?
    where_clauses = ["id = ?"]
    params.append(collection_id)
    if not bypass_cas and expected_version is not None and int(expected_version) > 0:
        where_clauses.append("version = ?")
        params.append(int(expected_version))

    set_sql = ", ".join(set_clauses)
    where_sql = " AND ".join(where_clauses)
    sql = f"UPDATE collections SET {set_sql} WHERE {where_sql}"

    try:
        async with store.transaction() as tx:
            cursor = await tx.execute(sql, tuple(params))
            affected = cursor.rowcount if cursor else 0
            if affected == 0:
                # 区分"集合不存在"与"版本冲突"
                check_cur = await tx.execute(
                    "SELECT version FROM collections WHERE id = ?",
                    (collection_id,),
                )
                check_row = await check_cur.fetchone()
                if not check_row:
                    raise AppError(
                        ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
                        params={
                            "collection_id": collection_id,
                            "reason": "collection_not_found",
                        },
                    )
                # 集合存在但 affected=0 → 版本冲突
                current_v = int(check_row[0] or 1)
                logger.info(
                    f"[collections] update_collection CAS 冲突 "
                    f"coll_id={collection_id} expected_v={expected_version} "
                    f"current_v={current_v}"
                )
                raise AppError(
                    ErrorCodes.COLLECTION_CAS_CONFLICT,
                    params={
                        "collection_id": collection_id,
                        "expected_version": int(expected_version) if expected_version else 0,
                        "current_version": current_v,
                    },
                )
            # 更新成功 → 写 dirty_outbox + 读取新版本
            await store.add_dirty_outbox(
                "collections", str(collection_id), connection=tx,
            )
            v_cur = await tx.execute(
                "SELECT version FROM collections WHERE id = ?",
                (collection_id,),
            )
            v_row = await v_cur.fetchone()
            new_v = int(v_row[0]) if v_row else 0
        logger.info(
            f"[collections] update_collection CAS 成功 "
            f"coll_id={collection_id} v{expected_version}→v{new_v} "
            f"bypass_cas={bypass_cas}"
        )
        return {
            "success": True, "conflict": False,
            "new_version": new_v, "current_version": new_v,
            "message": "更新成功",
        }
    except AppError:
        raise
    except Exception as e:
        logger.warning(f"[collections] update_collection 失败: {e}")
        raise AppError(
            ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
            params={
                "collection_id": collection_id,
                "reason": f"update_failed: {e}",
            },
        )


async def batch_retrieve(collection_id: int, user_id: int) -> dict:
    """R45 第 16 节: 批量取件 — 逐项校验权限与副本状态,返回部分结果。

    与 ``resolve_collection`` 的差异:
        - ``resolve_collection`` 仅返回校验结果(是否允许),不区分"项可取"vs"项存在"
        - ``batch_retrieve`` 显式区分每项的 retrieval_status:
            * ``retrievable`` — 权限通过 + 文件 active + 副本就绪
            * ``denied`` — 权限不通过(owner 不匹配 + uploader 不匹配)
            * ``expired`` — 文件已过期(权限可能通过,但无法取件)
            * ``deleted`` — 文件已删除(权限可能通过,但无法取件)
            * ``missing`` — 文件记录不存在(数据异常)
        - 不因个别项失败导致整体失败,调用方按 retrieval_status 决策

    权限规则(同 resolve_collection):
        - owner_id == user_id → 所有项 allowed
        - 其他用户 → 校验 file_records_local.uploader_id == user_id

    Args:
        collection_id: 集合 ID
        user_id: 当前访问用户 ID

    Returns:
        {
            "collection_id": int,
            "collection_code": str,
            "collection_name": str,
            "owner_id": int,
            "is_owner": bool,             # user_id 是否为集合 owner
            "items": [                    # 所有项的详细状态
                {
                    "file_code": str,
                    "added_at": str,
                    "retrieval_status": str,  # retrievable/denied/expired/deleted/missing
                    "uploader_id": int,
                    "has_access": bool,       # 权限是否通过
                    "reason": str,             # 失败原因(可读)
                },
                ...
            ],
            "retrievable_count": int,    # 可取件数量
            "denied_count": int,
            "expired_count": int,
            "deleted_count": int,
            "missing_count": int,
            "total_count": int,
            "all_retrievable": bool,     # 所有项均可取件
            "error": str,                 # 集合不存在/数据库错误
        }
    """
    store = get_cache_store()
    if not store._db:
        return {
            "collection_id": collection_id,
            "collection_code": "", "collection_name": "",
            "owner_id": 0, "is_owner": False,
            "items": [], "retrievable_count": 0,
            "denied_count": 0, "expired_count": 0,
            "deleted_count": 0, "missing_count": 0,
            "total_count": 0, "all_retrievable": False,
            "error": "数据库未初始化",
        }
    try:
        # 查询集合信息
        coll_cur = await store._db.execute(
            """SELECT id, name, code, owner_id, status, item_count
               FROM collections WHERE id = ?""",
            (collection_id,),
        )
        coll_row = await coll_cur.fetchone()
        if not coll_row:
            return {
                "collection_id": collection_id,
                "collection_code": "", "collection_name": "",
                "owner_id": 0, "is_owner": False,
                "items": [], "retrievable_count": 0,
                "denied_count": 0, "expired_count": 0,
                "deleted_count": 0, "missing_count": 0,
                "total_count": 0, "all_retrievable": False,
                "error": "集合不存在",
            }
        coll_id = int(coll_row[0])
        coll_name = coll_row[1] or ""
        coll_code = coll_row[2] or ""
        owner_id = int(coll_row[3]) if coll_row[3] else 0
        coll_status = coll_row[4] or "active"
        is_owner = (owner_id == user_id)

        # 集合本身已禁用 → 全部不可取件
        if coll_status != "active":
            return {
                "collection_id": coll_id,
                "collection_code": coll_code,
                "collection_name": coll_name,
                "owner_id": owner_id, "is_owner": is_owner,
                "items": [], "retrievable_count": 0,
                "denied_count": 0, "expired_count": 0,
                "deleted_count": 0, "missing_count": 0,
                "total_count": 0, "all_retrievable": False,
                "error": "集合已禁用,无法取件",
            }

        # 查询集合项 + 关联文件状态
        items_cur = await store._db.execute(
            """SELECT ci.file_code, ci.added_at,
                      fr.uploader_id, fr.status, fr.deleted_at, fr.expire_time
               FROM collection_items ci
               LEFT JOIN file_records_local fr ON ci.file_code = fr.file_code
               WHERE ci.collection_id = ?
               ORDER BY ci.added_at ASC""",
            (coll_id,),
        )
        items_rows = await items_cur.fetchall()

        items: list[dict] = []
        retrievable_count = 0
        denied_count = 0
        expired_count = 0
        deleted_count = 0
        missing_count = 0

        for r in items_rows:
            file_code = r[0] or ""
            added_at = r[1] or ""
            uploader_id = int(r[2]) if r[2] else 0
            file_status = (r[3] or "missing").lower()
            deleted_at = r[4]
            expire_time = r[5]

            # 1. 权限校验
            if is_owner:
                has_access = True
            elif uploader_id == user_id and uploader_id != 0:
                has_access = True
            else:
                has_access = False

            # 2. 文件状态判定
            if not file_code or file_status == "missing":
                retrieval_status = "missing"
                reason = "文件记录不存在"
                missing_count += 1
            elif deleted_at or file_status == "deleted":
                retrieval_status = "deleted"
                reason = "文件已删除"
                deleted_count += 1
            elif file_status == "expired" or _is_expired(expire_time):
                retrieval_status = "expired"
                reason = "文件已过期"
                expired_count += 1
            elif not has_access:
                retrieval_status = "denied"
                reason = "无访问权限(uploader 不匹配)"
                denied_count += 1
            elif file_status in ("active", "ready"):
                retrieval_status = "retrievable"
                reason = ""
                retrievable_count += 1
            else:
                # 其他异常状态(如 corrupted)→ 视为不可取件
                retrieval_status = "deleted"
                reason = f"文件状态异常: {file_status}"
                deleted_count += 1

            items.append({
                "file_code": file_code,
                "added_at": added_at,
                "retrieval_status": retrieval_status,
                "uploader_id": uploader_id,
                "has_access": has_access,
                "reason": reason,
            })

        total_count = len(items)
        all_retrievable = (
            total_count > 0
            and retrievable_count == total_count
        )
        return {
            "collection_id": coll_id,
            "collection_code": coll_code,
            "collection_name": coll_name,
            "owner_id": owner_id,
            "is_owner": is_owner,
            "items": items,
            "retrievable_count": retrievable_count,
            "denied_count": denied_count,
            "expired_count": expired_count,
            "deleted_count": deleted_count,
            "missing_count": missing_count,
            "total_count": total_count,
            "all_retrievable": all_retrievable,
            "error": "",
        }
    except Exception as e:
        logger.warning(f"[collections] batch_retrieve 失败: {e}")
        return {
            "collection_id": collection_id,
            "collection_code": "", "collection_name": "",
            "owner_id": 0, "is_owner": False,
            "items": [], "retrievable_count": 0,
            "denied_count": 0, "expired_count": 0,
            "deleted_count": 0, "missing_count": 0,
            "total_count": 0, "all_retrievable": False,
            "error": f"批量取件失败: {e}",
        }
