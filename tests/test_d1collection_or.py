"""回归测试 8 —— session.D1Collection.find 完整翻译 $or 子句各操作符（P0 查询构造）。

验证 find 正确将 MongoDB 风格查询（含顶层字段与 $or 子条件中的 $gte/$lte/$gt/$lt/
$ne/$in/$regex）翻译为带参数占位符的 SQL，且表名/列名经 _validate_identifier 校验
（恶意标识符抛 ValueError，防 SQL 注入，P0-2）。
"""

import pytest

from database.session import D1Collection


async def test_d1collection_or_translates_all_operators(monkeypatch):
    captured = []

    async def _fake_query(self, sql, params=None):
        captured.append(sql)
        return []

    monkeypatch.setattr(D1Collection, "_query", _fake_query)

    query = {
        "$or": [
            {"a": {"$gte": 10}, "b": {"$lte": 20}},
            {"c": {"$gt": 5}, "d": {"$lt": 3}},
            {"e": {"$ne": 7}},
            {"f": {"$in": [1, 2, 3]}},
            {"g": {"$regex": "foo"}},
        ]
    }
    await D1Collection("cells").find(query)

    assert captured, "find 应执行查询并生成 SQL"
    sql = captured[0]
    # 顶层/子条件操作符均被正确翻译
    assert "a >= $" in sql
    assert "b <= $" in sql
    assert "c > $" in sql
    assert "d < $" in sql
    assert "e != $" in sql
    assert "f IN (" in sql
    assert "g LIKE $" in sql
    assert "ESCAPE '\\'" in sql
    # $or 子句以 OR 连接并整体括号包裹
    assert " OR " in sql
    assert sql.count("(") >= 2  # 整体 OR 组 + 各子条件 AND 组


async def test_d1collection_rejects_malicious_table_name():
    """表名含注入片段时，_validate_identifier 必须抛 ValueError（P0-2）。"""
    with pytest.raises(ValueError):
        # find 在拼接 FROM 子句时调用 _validate_identifier(table) 触发校验
        await D1Collection("cells; DROP TABLE cells; --").find({"x": 1})
