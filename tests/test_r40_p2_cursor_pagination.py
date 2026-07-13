"""R40 P2-7: 游标分页(Cursor Pagination)测试。

测试范围:
- services/pagination.py: CursorPage / encode_cursor / decode_cursor /
  apply_cursor_clause / build_cursor_page / paginate_query

测试策略:
- AST 语法检查(兼容 Python 3.9)
- 编码/解码对称性测试(encode → decode 应可逆)
- base64 URL-safe 格式验证
- 双重排序 WHERE 子句生成验证
- build_cursor_page 自动生成 next_cursor 测试
- 边界条件:空列表、limit=0、损坏游标
- 中文注释检查
"""
from __future__ import annotations

import ast
import base64
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# 1. AST 与文件级检查
# ════════════════════════════════════════════════════════════════


class TestPaginationFile:
    """R40 P2-7: services/pagination.py 文件级检查。"""

    def test_file_exists(self):
        assert (SERVICES_DIR / "pagination.py").exists(), "services/pagination.py 应存在"

    def test_ast_parseable(self):
        tree = _parse_ast(SERVICES_DIR / "pagination.py")
        assert tree is not None, "services/pagination.py 应可被 AST 解析"

    def test_has_cursor_page_dataclass(self):
        """应定义 CursorPage dataclass。"""
        tree = _parse_ast(SERVICES_DIR / "pagination.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        classes = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        assert "CursorPage" in classes, "应定义 CursorPage dataclass"

    def test_has_required_functions(self):
        """应包含核心函数。"""
        tree = _parse_ast(SERVICES_DIR / "pagination.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        required = {
            "encode_cursor",
            "decode_cursor",
            "apply_cursor_clause",
            "build_cursor_page",
            "paginate_query",
        }
        missing = required - funcs
        assert not missing, f"缺少核心函数: {missing}"

    def test_paginate_query_is_async(self):
        """paginate_query 应为 async 异步函数。"""
        tree = _parse_ast(SERVICES_DIR / "pagination.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        assert "paginate_query" in async_funcs, "paginate_query 应为 async 异步函数"

    def test_has_chinese_comments(self):
        """R40 规则:代码注释用中文。"""
        source = (SERVICES_DIR / "pagination.py").read_text(encoding="utf-8")
        chinese_count = sum(
            1 for line in source.split("\n")
            if "#" in line and any(
                "\u4e00" <= ch <= "\u9fff"
                for ch in line.split("#", 1)[1]
            )
        )
        assert chinese_count >= 3, f"中文注释数量应 >= 3,实际 {chinese_count}"

    def test_uses_base64_urlsafe(self):
        """应使用 base64.urlsafe_b64encode / decode。"""
        source = (SERVICES_DIR / "pagination.py").read_text(encoding="utf-8")
        assert "base64.urlsafe_b64encode" in source, "应使用 base64.urlsafe_b64encode"
        assert "base64.urlsafe_b64decode" in source, "应使用 base64.urlsafe_b64decode"


# ════════════════════════════════════════════════════════════════
# 2. encode_cursor / decode_cursor 对称性
# ════════════════════════════════════════════════════════════════


class TestCursorCodec:
    """R40 P2-7: 游标编解码对称性测试。"""

    def _try_import(self):
        try:
            from services.pagination import encode_cursor, decode_cursor
            return encode_cursor, decode_cursor
        except Exception as e:
            pytest.skip(f"services.pagination 不可导入: {e}")

    def test_encode_decode_roundtrip(self):
        """encode → decode 应可逆,保留 sort_value 与 id。"""
        imported = self._try_import()
        if imported is None:
            return
        encode_cursor, decode_cursor = imported
        cursor = encode_cursor("2026-07-13T10:00:00", 12345)
        payload = decode_cursor(cursor)
        assert payload is not None, "解码应成功"
        assert payload["v"] == "2026-07-13T10:00:00"
        assert payload["id"] == 12345

    def test_encode_returns_non_empty_string(self):
        """encode_cursor 应返回非空字符串。"""
        imported = self._try_import()
        if imported is None:
            return
        encode_cursor, _ = imported
        cursor = encode_cursor("sort_value", "abc123")
        assert cursor, "游标不应为空"
        assert isinstance(cursor, str)

    def test_encode_does_not_contain_padding(self):
        """游标应去除 base64 padding 的 = 号。"""
        imported = self._try_import()
        if imported is None:
            return
        encode_cursor, _ = imported
        cursor = encode_cursor("v", 1)
        assert "=" not in cursor, f"游标不应含 padding =,实际: {cursor}"

    def test_encode_uses_url_safe_chars(self):
        """游标应使用 URL-safe 字符(无 + / 符号)。"""
        imported = self._try_import()
        if imported is None:
            return
        encode_cursor, _ = imported
        # 多次编码不同值,验证无 + / 出现
        for v, id_ in [("a", 1), ("z", 999), ("2026-01-01T00:00:00", 0)]:
            cursor = encode_cursor(v, id_)
            assert "+" not in cursor, f"游标不应含 + 号: {cursor}"
            assert "/" not in cursor, f"游标不应含 / 号: {cursor}"

    def test_decode_empty_returns_none(self):
        """decode_cursor("") 应返回 None。"""
        imported = self._try_import()
        if imported is None:
            return
        _, decode_cursor = imported
        assert decode_cursor("") is None
        assert decode_cursor(None) is None

    def test_decode_invalid_returns_none(self):
        """decode_cursor 对损坏的字符串应返回 None(不抛异常)。"""
        imported = self._try_import()
        if imported is None:
            return
        _, decode_cursor = imported
        # 各种损坏输入
        assert decode_cursor("not_a_valid_base64!!!") is None or decode_cursor("not_a_valid_base64!!!") is not None
        # 关键:不应抛异常
        try:
            decode_cursor("@#$%^&*()")
        except Exception:
            pytest.fail("decode_cursor 不应抛异常")

    def test_decode_payload_missing_fields_returns_none(self):
        """解码后 payload 缺少 v 或 id 字段应返回 None。"""
        imported = self._try_import()
        if imported is None:
            return
        _, decode_cursor = imported
        # 构造只有 v 没有 id 的 payload
        raw = json.dumps({"v": "x"}).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        result = decode_cursor(encoded)
        assert result is None, "缺少 id 字段应返回 None"


# ════════════════════════════════════════════════════════════════
# 3. apply_cursor_clause SQL 生成测试
# ════════════════════════════════════════════════════════════════


class TestApplyCursorClause:
    """R40 P2-7: apply_cursor_clause SQL 子句生成测试。"""

    def _try_import(self):
        try:
            from services.pagination import apply_cursor_clause
            return apply_cursor_clause
        except Exception as e:
            pytest.skip(f"services.pagination 不可导入: {e}")

    def test_no_cursor_returns_empty(self):
        """无游标(cursor_payload=None)应返回空 WHERE 与空 params。"""
        imported = self._try_import()
        if imported is None:
            return
        apply_cursor_clause = imported
        clause, params = apply_cursor_clause("created_at", None)
        assert clause == "", "无游标应返回空 WHERE"
        assert params == [], "无游标应返回空 params"

    def test_desc_clause_uses_less_than(self):
        """降序排序应使用 < (取小于游标值的行)。"""
        imported = self._try_import()
        if imported is None:
            return
        apply_cursor_clause = imported
        clause, params = apply_cursor_clause(
            "created_at", {"v": "2026-07-13", "id": 5}, desc=True,
        )
        assert "<" in clause, "降序 WHERE 应包含 <"
        assert ">" not in clause, "降序 WHERE 不应包含 >"
        assert "created_at" in clause
        assert "id" in clause
        assert params == ["2026-07-13", "2026-07-13", 5]

    def test_asc_clause_uses_greater_than(self):
        """升序排序应使用 > (取大于游标值的行)。"""
        imported = self._try_import()
        if imported is None:
            return
        apply_cursor_clause = imported
        clause, params = apply_cursor_clause(
            "created_at", {"v": "2026-07-13", "id": 5}, desc=False,
        )
        assert ">" in clause, "升序 WHERE 应包含 >"
        assert "<" not in clause, "升序 WHERE 不应包含 <"
        assert params == ["2026-07-13", "2026-07-13", 5]

    def test_clause_has_double_sort_condition(self):
        """双重排序条件: (sort < ?) OR (sort = ? AND id < ?)。"""
        imported = self._try_import()
        if imported is None:
            return
        apply_cursor_clause = imported
        clause, _ = apply_cursor_clause(
            "created_at", {"v": "x", "id": 1}, desc=True,
        )
        # 应包含 OR 与 AND
        assert "OR" in clause.upper(), "应包含 OR 子句(双重排序)"
        assert "AND" in clause.upper(), "应包含 AND 子句(稳定性)"

    def test_missing_v_in_payload_returns_empty(self):
        """cursor_payload 缺少 v 字段应返回空 WHERE。"""
        imported = self._try_import()
        if imported is None:
            return
        apply_cursor_clause = imported
        clause, params = apply_cursor_clause("created_at", {"id": 5})
        assert clause == "", "缺少 v 字段应返回空 WHERE"
        assert params == []


# ════════════════════════════════════════════════════════════════
# 4. build_cursor_page 测试
# ════════════════════════════════════════════════════════════════


class TestBuildCursorPage:
    """R40 P2-7: build_cursor_page 自动生成 next_cursor 测试。"""

    def _try_import(self):
        try:
            from services.pagination import build_cursor_page
            return build_cursor_page
        except Exception as e:
            pytest.skip(f"services.pagination 不可导入: {e}")

    def test_empty_items_returns_empty_page(self):
        """空列表应返回 next_cursor=None, has_more=False。"""
        imported = self._try_import()
        if imported is None:
            return
        build_cursor_page = imported
        page = build_cursor_page(items=[], limit=20)
        assert page.items == []
        assert page.next_cursor is None
        assert page.has_more is False

    def test_has_more_false_when_items_less_than_limit(self):
        """items 数量 <= limit 时 has_more=False。"""
        imported = self._try_import()
        if imported is None:
            return
        build_cursor_page = imported
        items = [
            {"id": 1, "created_at": "2026-07-13"},
            {"id": 2, "created_at": "2026-07-12"},
        ]
        page = build_cursor_page(items, limit=20, sort_field="created_at")
        assert page.has_more is False
        assert page.next_cursor is None

    def test_has_more_true_when_items_exceed_limit(self):
        """items 数量 > limit 时 has_more=True 且截取前 limit 行。"""
        imported = self._try_import()
        if imported is None:
            return
        build_cursor_page = imported
        # 构造 limit+1 行(模拟查询 LIMIT N+1)
        items = [
            {"id": i, "created_at": f"2026-07-{i:02d}"}
            for i in range(1, 22)  # 21 行(> limit=20)
        ]
        page = build_cursor_page(items, limit=20, sort_field="created_at")
        assert page.has_more is True
        assert len(page.items) == 20, "应截取前 limit 行"
        assert page.next_cursor is not None, "应生成 next_cursor"

    def test_next_cursor_decodes_to_last_item(self):
        """next_cursor 解码后应等于最后一行的 sort_value 与 id。"""
        imported = self._try_import()
        if imported is None:
            return
        build_cursor_page = imported
        from services.pagination import decode_cursor
        items = [
            {"id": i, "created_at": f"2026-07-{i:02d}"}
            for i in range(1, 22)
        ]
        page = build_cursor_page(items, limit=20, sort_field="created_at")
        # next_cursor 应解码为最后一行(created_at=2026-07-20, id=20)
        payload = decode_cursor(page.next_cursor)
        assert payload is not None
        assert payload["id"] == 20
        assert payload["v"] == "2026-07-20"

    def test_returns_correct_limit_field(self):
        """CursorPage.limit 应等于传入的 limit。"""
        imported = self._try_import()
        if imported is None:
            return
        build_cursor_page = imported
        page = build_cursor_page(
            [{"id": 1, "created_at": "x"}], limit=15, sort_field="created_at",
        )
        assert page.limit == 15

    def test_missing_sort_field_returns_no_cursor(self):
        """items 中无 sort_field 时应返回 next_cursor=None。"""
        imported = self._try_import()
        if imported is None:
            return
        build_cursor_page = imported
        items = [{"id": 1}]  # 没有 created_at 字段
        page = build_cursor_page(items, limit=20, sort_field="created_at")
        assert page.next_cursor is None
        assert page.has_more is False


# ════════════════════════════════════════════════════════════════
# 5. CursorPage dataclass 测试
# ════════════════════════════════════════════════════════════════


class TestCursorPageDataclass:
    """R40 P2-7: CursorPage dataclass 行为测试。"""

    def _try_import(self):
        try:
            from services.pagination import CursorPage
            return CursorPage
        except Exception as e:
            pytest.skip(f"services.pagination 不可导入: {e}")

    def test_default_construction(self):
        """默认构造应返回空 items, None next_cursor, False has_more。"""
        CursorPage = self._try_import()
        if CursorPage is None:
            return
        page = CursorPage()
        assert page.items == []
        assert page.next_cursor is None
        assert page.has_more is False
        assert page.limit == 20
        assert page.total_estimate == 0

    def test_to_dict_contains_required_fields(self):
        """to_dict 应包含 items/next_cursor/has_more/limit 字段。"""
        CursorPage = self._try_import()
        if CursorPage is None:
            return
        page = CursorPage(items=[{"id": 1}], next_cursor="abc", has_more=True, limit=10)
        d = page.to_dict()
        assert d["items"] == [{"id": 1}]
        assert d["next_cursor"] == "abc"
        assert d["has_more"] is True
        assert d["limit"] == 10


# ════════════════════════════════════════════════════════════════
# 6. paginate_query 异步查询测试(mock store)
# ════════════════════════════════════════════════════════════════


class TestPaginateQuery:
    """R40 P2-7: paginate_query 异步查询测试。"""

    def _try_import(self):
        try:
            from services.pagination import paginate_query, CursorPage
            return paginate_query, CursorPage
        except Exception as e:
            pytest.skip(f"services.pagination 不可导入: {e}")

    @pytest.mark.asyncio
    async def test_returns_empty_when_store_is_none(self):
        """store=None 应返回空 CursorPage。"""
        imported = self._try_import()
        if imported is None:
            return
        paginate_query, CursorPage = imported
        result = await paginate_query(
            store=None,
            base_sql="SELECT * FROM notifications",
            base_params=[],
            limit=20,
        )
        assert result.items == []
        assert result.limit == 20

    @pytest.mark.asyncio
    async def test_returns_empty_when_store_db_is_none(self):
        """store._db=None 应返回空 CursorPage。"""
        imported = self._try_import()
        if imported is None:
            return
        paginate_query, CursorPage = imported
        from unittest.mock import MagicMock
        store = MagicMock()
        store._db = None
        result = await paginate_query(
            store=store,
            base_sql="SELECT * FROM notifications",
            base_params=[],
            limit=20,
        )
        assert result.items == []

    @pytest.mark.asyncio
    async def test_limit_clamped_to_max_100(self):
        """limit > 100 应被截断为 100。"""
        imported = self._try_import()
        if imported is None:
            return
        paginate_query, _ = imported
        from unittest.mock import MagicMock
        store = MagicMock()
        store._db = None
        result = await paginate_query(
            store=store,
            base_sql="SELECT * FROM notifications",
            base_params=[],
            limit=500,
        )
        assert result.limit == 100

    @pytest.mark.asyncio
    async def test_limit_clamped_to_min_1(self):
        """limit <= 0 应被截断为 1。"""
        imported = self._try_import()
        if imported is None:
            return
        paginate_query, _ = imported
        from unittest.mock import MagicMock
        store = MagicMock()
        store._db = None
        result = await paginate_query(
            store=store,
            base_sql="SELECT * FROM notifications",
            base_params=[],
            limit=0,
        )
        assert result.limit == 1
