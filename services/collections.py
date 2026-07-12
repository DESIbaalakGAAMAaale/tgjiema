"""R40 §9.1.3: 文件集合 — 合集码 + 批量下载 + 版本管理。

为用户提供文件合集功能,可批量管理和下载多个文件。
支持集合更新版本号、部分失效提示等。

设计要点:
- create_collection 调用 code_generator.build_collection_code() 生成唯一集合码
  (复用现有 code_generator 模块)
- add_files / remove_files 用 INSERT OR IGNORE / DELETE 批量操作 + 更新 item_count + 调用 update_version
- get_collection 联合查询 collections + collection_items + file_records_local
  (检查文件是否失效: deleted_at / status / expire_time)
- 每次写入后调用 add_dirty_outbox(table_name, pk) 触发 CRDB 同步
- 通过 get_cache_store() 获取 CacheStore 单例
"""
import datetime as _dt
import json
from typing import Any

from loguru import logger

from database.cache_store import get_cache_store
from services.code_generator import build_collection_code


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
        cursor = await store._db.execute(
            """INSERT INTO collections (name, code, owner_id, description, version,
                                         item_count, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, 0, 'active', ?, ?)""",
            (name, code, owner_id, description, now, now),
        )
        await store._db.commit()
        coll_id = int(cursor.lastrowid) if cursor and cursor.lastrowid else 0
        if coll_id:
            await store.add_dirty_outbox("collections", str(coll_id))
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
        # 使用 INSERT OR IGNORE 去重(collection_items 表有唯一索引更好,
        # 当前 schema 无唯一约束,先查再插避免重复)
        for code in file_codes:
            # 检查是否已存在
            chk = await store._db.execute(
                "SELECT id FROM collection_items WHERE collection_id = ? AND file_code = ?",
                (collection_id, code),
            )
            existing = await chk.fetchone()
            if existing:
                continue
            cursor = await store._db.execute(
                """INSERT INTO collection_items (collection_id, file_code, added_at)
                   VALUES (?, ?, ?)""",
                (collection_id, code, now),
            )
            if cursor and cursor.rowcount > 0:
                added += 1
        # 更新 item_count + updated_at
        if added > 0:
            await store._db.execute(
                """UPDATE collections
                   SET item_count = (
                        SELECT COUNT(*) FROM collection_items WHERE collection_id = ?
                       ),
                       updated_at = ?
                   WHERE id = ?""",
                (collection_id, now, collection_id),
            )
            await store._db.commit()
            await store.add_dirty_outbox("collections", str(collection_id))
            # collection_items 变更通过 collections pk 同步(简化)
            await store.add_dirty_outbox("collection_items", str(collection_id))
            # 调用 update_version 升级版本号
            await update_version(collection_id)
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
        for code in file_codes:
            cursor = await store._db.execute(
                """DELETE FROM collection_items
                   WHERE collection_id = ? AND file_code = ?""",
                (collection_id, code),
            )
            if cursor and cursor.rowcount > 0:
                removed += 1
        if removed > 0:
            # 更新 item_count + updated_at
            await store._db.execute(
                """UPDATE collections
                   SET item_count = (
                        SELECT COUNT(*) FROM collection_items WHERE collection_id = ?
                       ),
                       updated_at = ?
                   WHERE id = ?""",
                (collection_id, now, collection_id),
            )
            await store._db.commit()
            await store.add_dirty_outbox("collections", str(collection_id))
            await store.add_dirty_outbox("collection_items", str(collection_id))
            # 升级版本号
            await update_version(collection_id)
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


async def list_collections(owner_id: int, page: int = 1, page_size: int = 10) -> dict:
    """分页列出用户的集合。

    Args:
        owner_id: 所有者用户 ID
        page: 页码(从 1 开始)
        page_size: 每页条数(1-100)

    Returns:
        {items, total, page, page_size, total_pages}
    """
    store = get_cache_store()
    default = {
        "items": [], "total": 0,
        "page": page, "page_size": page_size, "total_pages": 0,
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
        offset = (page - 1) * page_size
        cursor = await store._db.execute(
            """SELECT id, name, code, owner_id, description, version,
                      item_count, status, created_at, updated_at
               FROM collections WHERE owner_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (owner_id, page_size, offset),
        )
        rows = await cursor.fetchall()
        items = [
            {
                "id": r[0], "name": r[1], "code": r[2], "owner_id": r[3],
                "description": r[4], "version": int(r[5] or 1),
                "item_count": int(r[6] or 0), "status": r[7],
                "created_at": r[8], "updated_at": r[9],
            }
            for r in rows
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    except Exception as e:
        logger.warning(f"[collections] list_collections 失败: {e}")
        return default


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
        # 原子递增 version + 更新 updated_at
        cursor = await store._db.execute(
            """UPDATE collections
               SET version = version + 1, updated_at = ?
               WHERE id = ?""",
            (now, collection_id),
        )
        await store._db.commit()
        if cursor and cursor.rowcount > 0:
            await store.add_dirty_outbox("collections", str(collection_id))
            # 查询新版本号
            v_cursor = await store._db.execute(
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
