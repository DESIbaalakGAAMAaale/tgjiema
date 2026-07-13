#!/usr/bin/env python3
"""R45 评估报告 4.4: 验证 file_records(status) 索引是否被真实查询使用。

执行 EXPLAIN ANALYZE 检查 status 索引使用情况,根据结果决定是否删除。

用法:
    python scripts/verify_file_records_status_index.py
    
输出:
    - EXPLAIN ANALYZE 结果
    - 索引使用建议(保留/删除/改为部分索引)
"""
import asyncio
import os
import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def verify_status_index_usage():
    """执行 EXPLAIN ANALYZE 检查 status 索引使用情况。"""
    crdb_url = os.getenv("COCKROACHDB_URL")
    if not crdb_url:
        print("❌ COCKROACHDB_URL 未配置,无法执行 EXPLAIN ANALYZE")
        print("   请在 .env 中配置 COCKROACHDB_URL 后重试")
        return 1
    
    try:
        import asyncpg
    except ImportError:
        print("❌ asyncpg 未安装")
        return 1
    
    conn = await asyncpg.connect(crdb_url)
    try:
        # 检查 idx_file_records_status 是否存在
        exists = await conn.fetchval(
            "SELECT count(*) FROM information_schema.statistics "
            "WHERE table_name = 'file_records' AND index_name = 'idx_file_records_status'"
        )
        if not exists:
            print("✓ idx_file_records_status 不存在,无需验证")
            return 0
        
        # 执行 EXPLAIN ANALYZE 检查典型查询
        queries = [
            ("按状态查活跃文件", "SELECT * FROM file_records WHERE status = 'active' LIMIT 10"),
            ("按状态查下架文件", "SELECT * FROM file_records WHERE status = 'deleted' LIMIT 10"),
            ("按状态计数", "SELECT status, count(*) FROM file_records GROUP BY status"),
        ]
        
        print("=" * 70)
        print("file_records(status) 索引使用验证")
        print("=" * 70)
        
        for name, sql in queries:
            print(f"\n{'─' * 50}")
            print(f"查询: {name}")
            print(f"SQL: {sql}")
            print('─' * 50)
            try:
                rows = await conn.fetch(f"EXPLAIN ANALYZE {sql}")
                for row in rows:
                    print(row[0])
            except Exception as e:
                print(f"执行失败: {e}")
        
        print(f"\n{'=' * 70}")
        print("判定建议:")
        print("  - 如果上述查询都未使用 idx_file_records_status → 建议删除")
        print("  - 如果有查询使用且选择性足够 → 保留")
        print("  - 如果选择性低(大部分为 active) → 改为部分索引 WHERE status = 'active'")
        print("=" * 70)
        return 0
    finally:
        await conn.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(verify_status_index_usage()))
