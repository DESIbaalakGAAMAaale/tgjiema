"""R40 P2-7: 游标分页(Cursor Pagination)。

职责:
    替代 offset/limit 分页(大数据集性能差),改为基于游标的稳定分页:
    1. CursorPage — 分页结果数据类(items + next_cursor + has_more)
    2. encode_cursor / decode_cursor — 游标编解码(base64 URL-safe)
    3. apply_cursor — 给定查询参数与游标,返回带 WHERE + LIMIT 的查询
    4. build_cursor_page — 包装查询结果为 CursorPage

设计原则:
    - 游标格式: base64(JSON({"sort_field": value, "id": id}))
      sort_field 是排序字段值(如 created_at)
      id 是稳定唯一键(避免相同 sort_field 时丢行)
    - 双重排序: ORDER BY sort_field DESC, id DESC(稳定性保证)
    - 游标不可伪造(可加 HMAC 签名,后续接入)
    - 兼容现有 list_* 函数(可独立使用,无需改动 SQLite 查询)
    - 中文注释,loguru 日志

使用示例:
    # 1. 第一页(无游标)
    page = await list_notifications_cursor(user_id=123, limit=20)
    # page.next_cursor 可传给下一页请求

    # 2. 后续页(带游标)
    next_cursor = request.query_params.get("cursor")
    page = await list_notifications_cursor(
        user_id=123, limit=20, cursor=next_cursor,
    )

    # 3. 路由响应
    return {
        "items": page.items,
        "next_cursor": page.next_cursor,  # None 表示无下一页
        "has_more": page.has_more,
    }
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger


@dataclass
class CursorPage:
    """R40 P2-7: 游标分页结果。

    字段:
        items: 当前页数据列表
        next_cursor: 下一页游标(None 表示无下一页)
        has_more: 是否还有更多数据
        limit: 本次查询的页大小
        total_estimate: 估算总数(可选,0=未估算)
    """
    items: list = field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False
    limit: int = 20
    total_estimate: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 响应字典。"""
        return {
            "items": self.items,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "limit": self.limit,
            "total_estimate": self.total_estimate,
        }


def encode_cursor(sort_value: Any, item_id: Any) -> str:
    """编码游标为 base64 URL-safe 字符串。

    Args:
        sort_value: 排序字段值(如 created_at 字符串或时间戳)
        item_id: 唯一 ID(用于相同 sort_value 时的稳定排序)

    Returns:
        base64 URL-safe 字符串(无 padding)
    """
    payload = {"v": sort_value, "id": item_id}
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    # URL-safe base64(去除 padding 的 =,缩短游标长度)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return encoded


def decode_cursor(cursor: str) -> Optional[dict[str, Any]]:
    """解码游标。

    Args:
        cursor: base64 URL-safe 字符串

    Returns:
        {"v": sort_value, "id": item_id} 字典;失败返回 None
    """
    if not cursor:
        return None
    try:
        # 补齐 padding(== 或 =)
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if "v" not in payload or "id" not in payload:
            return None
        return payload
    except (ValueError, json.JSONDecodeError, TypeError, base64.binascii.Error) as e:
        logger.debug(f"[pagination] 游标解码失败: {e}")
        return None


def apply_cursor_clause(
    sort_field: str,
    cursor_payload: Optional[dict[str, Any]],
    desc: bool = True,
) -> tuple[str, list[Any]]:
    """生成游标过滤的 SQL WHERE 子句(用于游标分页查询)。

    双重排序场景:
        ORDER BY sort_field DESC, id DESC
        下一页游标: WHERE (sort_field < cursor.v) OR
                          (sort_field = cursor.v AND id < cursor.id)

    Args:
        sort_field: 排序字段名(如 "created_at")
        cursor_payload: 解码后的游标 {"v":..., "id":...}(None 表示第一页)
        desc: 是否降序(True=DESC,False=ASC)

    Returns:
        (where_clause, params) — where_clause 为 SQL 片段(可拼接到主查询),
        params 为参数列表(用于 ? 占位符)
    """
    if cursor_payload is None:
        return "", []
    sort_value = cursor_payload.get("v")
    item_id = cursor_payload.get("id")
    if sort_value is None or item_id is None:
        return "", []
    # 双重排序:相同 sort_value 时按 id 排序保证稳定
    if desc:
        # 降序:取小于游标值的行
        clause = (
            f"WHERE ({sort_field} < ?) OR "
            f"({sort_field} = ? AND id < ?)"
        )
    else:
        # 升序:取大于游标值的行
        clause = (
            f"WHERE ({sort_field} > ?) OR "
            f"({sort_field} = ? AND id > ?)"
        )
    params = [sort_value, sort_value, item_id]
    return clause, params


def build_cursor_page(
    items: list,
    limit: int,
    sort_field: str = "created_at",
    id_field: str = "id",
    desc: bool = True,
) -> CursorPage:
    """根据查询结果构造 CursorPage(自动生成 next_cursor)。

    Args:
        items: 查询返回的当前页数据(list of dict)
        limit: 本次查询的页大小(用于判断 has_more)
        sort_field: 用于游标的排序字段名(如 "created_at")
        id_field: 数据中 ID 字段名(默认 "id")
        desc: 是否降序排序(与 ORDER BY 方向一致)

    Returns:
        CursorPage(items, next_cursor, has_more, limit)
    """
    if not items:
        return CursorPage(items=[], next_cursor=None, has_more=False, limit=limit)
    # 若返回行数 > limit,说明有更多数据
    # 调用方应传 limit+1 行(常见技巧:查询 LIMIT N+1,仅返回前 N 行)
    has_more = len(items) > limit
    if has_more:
        # 截取前 limit 行
        items = items[:limit]
        # 仅在还有下一页时,取最后一行作为 next_cursor 的依据
        last_item = items[-1]
        sort_value = last_item.get(sort_field) if isinstance(last_item, dict) else None
        item_id = last_item.get(id_field) if isinstance(last_item, dict) else None
        if sort_value is None or item_id is None:
            # 数据中没有排序字段或 ID,无法构造游标
            return CursorPage(
                items=items, next_cursor=None,
                has_more=False, limit=limit,
            )
        next_cursor = encode_cursor(sort_value, item_id)
        return CursorPage(
            items=items, next_cursor=next_cursor,
            has_more=True, limit=limit,
        )
    # has_more=False:无下一页,next_cursor 必须为 None
    return CursorPage(
        items=items, next_cursor=None,
        has_more=False, limit=limit,
    )


async def paginate_query(
    store,
    base_sql: str,
    base_params: list[Any],
    sort_field: str = "created_at",
    cursor: Optional[str] = None,
    limit: int = 20,
    desc: bool = True,
) -> CursorPage:
    """通用游标分页查询(供 SQLite 查询复用)。

    Args:
        store: CacheStore 实例(需有 _db 属性)
        base_sql: 基础 SELECT 查询(不含 WHERE/ORDER BY/LIMIT)
                 示例: "SELECT id, user_id, type FROM notifications"
        base_params: 基础查询的参数(用于已有 WHERE 条件)
        sort_field: 排序字段(默认 "created_at")
        cursor: 游标字符串(None=第一页)
        limit: 页大小(1-100)
        desc: 是否降序

    Returns:
        CursorPage
    """
    limit = max(1, min(100, int(limit)))
    if not store or not getattr(store, "_db", None):
        return CursorPage(items=[], limit=limit)
    # 解码游标
    cursor_payload = decode_cursor(cursor) if cursor else None
    where_clause, cursor_params = apply_cursor_clause(
        sort_field, cursor_payload, desc=desc,
    )
    # 拼接 SQL — 查询 limit+1 行用于判断 has_more
    # 注意:base_sql 已含 WHERE 时游标 WHERE 需改为 AND
    if "WHERE" in base_sql.upper():
        # 已有 WHERE,游标条件改为 AND
        cursor_clause = where_clause.replace("WHERE", "AND", 1) if where_clause else ""
    else:
        cursor_clause = where_clause
    order_dir = "DESC" if desc else "ASC"
    sql = (
        f"{base_sql} {cursor_clause} "
        f"ORDER BY {sort_field} {order_dir}, id {order_dir} "
        f"LIMIT ?"
    )
    params = list(base_params) + list(cursor_params) + [limit + 1]
    try:
        rows = await store._db.execute_fetchall(sql, tuple(params))
        # 将 row 转为 dict(假设 SELECT 字段顺序与 dict key 对应)
        # 调用方应自行解析 row,这里返回原始 row
        items = [dict(row) if hasattr(row, "keys") else list(row) for row in rows]
        page = build_cursor_page(items, limit, sort_field=sort_field, desc=desc)
        return page
    except Exception as e:
        logger.warning(f"[pagination] 游标分页查询失败: {e}")
        return CursorPage(items=[], limit=limit)
